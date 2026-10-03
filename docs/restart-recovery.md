# Task 9.4｜进程重启恢复验证记录

结论：**Python 重启、Go 重启、两者同时重启之后，thread 历史、当前任务、当前稿件、generation provider_task_id 全部可恢复。** 四次 HTTP 实测均 PASS。

---

## 1. 验证目标

Task 9.4 要求专门验证：

```text
Python 重启
Go 重启
两者都重启
```

之后以下四项仍可恢复：

```text
thread history
current task
current draft
generation provider_task_id
```

## 2. 验证环境

| 项目 | 值 |
| --- | --- |
| Go 服务 | `127.0.0.1:8909`（升级版二进制，`KOUBO_DATA_DIR=%TEMP%\koubo-p9\go`） |
| Python 服务 | `127.0.0.1:8090`（`DB_PATH=%TEMP%\koubo-p9\agent.sqlite`） |
| Checkpointer | `%TEMP%\koubo-p9\checkpoints.sqlite`（`AsyncSqliteSaver`，`thread_id = session_id`） |
| 会话 | `seed-session-1`（`task_id = task_seed-session-1`） |
| 固定数据 | `scripts/seed_phase8_session.py` 写入 |
| 检查脚本 | `scripts/check_recovery.py`，对 Go 公开接口 `GET /api/agent/session/seed-session-1` 断言 |

固定数据包含一个有真实历史的会话：

```text
thread history      : 6 条 message（3 轮 Human/AI）
current task        : task_seed-session-1（content_tasks 行）
current topic       : topic_seed_b（display_index=1）
current draft       : draft_seed_1 @ v1（正文 102 字，1 个版本）
active review       : review_seed_1（status=pass，绑定 v1）
generation          : generation_seed_1（status=pending，provider_task_id=provider_seed_1）
candidate cards     : 3 张（display_index 1/2/3）
```

## 3. 步骤与结果

每一步都重启**真实进程**，不做任何内存态搬运。

| 步骤 | 操作 | 结果 |
| --- | --- | --- |
| step 0 | 重启前基线 | **PASS** topic=topic_seed_b draft=draft_seed_1@v1 review=review_seed_1 generation=generation_seed_1 provider=provider_seed_1 messages=6 cards=3 |
| step A | kill 8090 → 重新启动 Python（Go 不动） | **PASS** 同上，逐字段一致 |
| step B | kill 8909 → 重新启动 Go（Python 已重启过） | **PASS** 同上，逐字段一致；`/agent.html` 同时 200 |
| step C | 同时 kill 8909 + 8090 → 两个进程都重新启动 | **PASS** 同上，逐字段一致 |

四次断言覆盖：`topic.topic_id`、`draft.draft_id`、`draft.current_version`、`review.review_id`、`generation.generation_id`、`generation.provider_task_id`、`message_count`（thread 历史条数）、`candidate_batch.candidates` 数量（display mapping）。

## 4. 恢复为什么成立（责任边界）

```text
thread history / 指针
  → LangGraph AsyncSqliteSaver（checkpoints.sqlite），按 thread_id 持久化，
    与进程生命周期无关

current task / draft / review / generation
  → Business SQLite（agent.sqlite），由 TaskStore / ReviewStore 直接读取

provider_task_id
  → generation_records 行内的字段；Go 侧同一 id 另有一条本地 task 记录，
    供轮询与状态查询复用

Go 重启
  → Go 不持有 Agent 会话状态：它只把 /api/agent/* 代理到 Python；
    自身只持有静态资源与本地任务文件
```

因此恢复不依赖任何进程内缓存，也不依赖“重启前先落盘”的额外步骤。

## 5. 复现命令

```powershell
# 1) 准备固定会话（使用与运行服务相同的 DB_PATH）
$env:DB_PATH = "$env:TEMP\koubo-p9\agent.sqlite"
python scripts/seed_phase8_session.py

# 2) 启动两个服务（分别一个终端）
$env:KOUBO_PORT='8909'; $env:AGENT_SERVICE_BASE_URL='http://127.0.0.1:8090'; .\koubo-video-tool-agent-upgrade.exe
python -m uvicorn agent_service.app:app --host 127.0.0.1 --port 8090

# 3) 基线检查
python scripts/check_recovery.py step0-before-restart

# 4) 重启任意进程后重复第 3 步即可
```

## 6. 结论

```text
TASK 9.4 RESTART RECOVERY: PASS
- Python 重启：thread history / current task / current draft / provider_task_id 全部恢复
- Go 重启：同上
- 两者同时重启：同上
```
