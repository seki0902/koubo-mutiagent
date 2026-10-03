# koubo-mutiagent

招聘选题 → 写稿 → 核实 → 审核 → 提交数字人成片，一套跑在本机的多 Agent 应用。

**这个仓库只包含 Agent 应用。** 原口播工具（形象/人声选择页、任务界面、批量生成那套）
的页面、脚本、文档与形象预览图都没有包含在内。

## 组成

| 部分 | 位置 | 作用 |
| --- | --- | --- |
| Go 网关 | `main.go`、`handlers/`、`store/`、`topicsearch/`、`chanjing/`、`llm/`、`scheduler/` | 端口 **8909**：前端页面、设置、会话流（SSE）、联网搜索与数字人提交能力 |
| Python Agent 服务 | `agent_service/` | 端口 **8090**：LangGraph 多 Agent（主管 + 选题/写稿/核实/审核四个专家） |
| 前端 | `frontend/` | `agent.html` 对话页、`settings.html` 设置页（`index.html` 为壳） |
| 写稿技能 | `agent_service/skill/` | `SKILL.md` + `docs/` + 6 个类库，按选题标题选类库 |

主管自己也能提交视频：`generate_video` 工具调用 **和原工具按钮完全相同**的
`POST /api/tasks`（`list_digital_humans` 列形象/人声，`check_video_status` 查进度）。
提交走直连路径，不经过授权/幂等表；服务方任务号存在会话状态的 `video_task_id` 里。

## 运行

需要 Go 1.22+ 与 Python 3.11+。

```powershell
# 1) 建虚拟环境并装依赖
python -m venv agent_service\.venv
agent_service\.venv\Scripts\pip install -r agent_service\requirements.txt

# 2) 一键启动（也可直接双击 start-agent.bat）
powershell -ExecutionPolicy Bypass -File .\start-agent.ps1
```

启动后打开 <http://127.0.0.1:8909/>，先在**设置页**填好大模型 API（DeepSeek 等）
与蝉镜 AppID / SecretKey，再回对话页提问。

- 停止：`stop-agent.ps1`
- 数据与配置默认写在 `%TEMP%\koubo-video-tool-agent-upgrade\`（可用
  `KOUBO_DATA_DIR`、`KOUBO_CONFIG_PATH`、`DB_PATH` 改写），**不在仓库里**。
- 密钥以 AES-256-GCM 加密后存盘，`GET /api/settings` 只返回掩码。

## 已知限制

- 主管的思考模式默认关闭（每轮省 10–20 秒），可在设置页开启；四个结构化输出的
  专家无法开启——思考模式会拒绝它们依赖的"强制工具调用"（服务方返回 400）。
- 每轮联网搜索次数有上限（`SUPERVISOR_SEARCH_LIMIT`，默认 2 次），达上限时结果里会
  写明"可能不全"。
- 提交后进度由调用方查询（`check_video_status`），没有后台等待或回调。

## 测试

仓库里**不含测试**（按交付要求剥离）。上游开发仓库保留完整测试集。
