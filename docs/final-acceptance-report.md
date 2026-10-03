# MVP 最终验收报告（Phase 9 / Task 9.5 冻结）

本报告是 V6.1 文档 Phase 0～9 的最终验收记录。**报告完成即项目 MVP 冻结**：不再继续扩需求、不再顺手优化。

工作区：`D:\koubo-video-tool\koubo-video-tool-agent-upgrade`（升级独立工作区，端口 8909 / 8090）
文档基线：`AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md`

---

## 1. Phase 9 五个 Task 的验收结论

| Task | 内容 | 结论 | 证据 |
| --- | --- | --- | --- |
| 9.1 | 单元测试与 Contract Test 补齐 | **PASS** | 文档要求的 7 项覆盖全部已有测试（见 §2），无缺口需要新增 |
| 9.2 | 并发与串行 Session 测试 | **PASS** | `agent_service/tests/test_concurrent_sessions.py`（3 个测试） |
| 9.3 | 故障注入 | **PASS** | `agent_service/tests/test_failure_injection.py`（10 个测试，覆盖文档列出的 9 类故障） |
| 9.4 | 进程重启恢复 | **PASS** | 真实进程重启矩阵 4/4 PASS，见 `docs/restart-recovery.md` |
| 9.5 | 最终 E2E Case 冻结 | **PASS（Case A 未实测，见 §5）** | `docs/demo-script.md` + 本报告 |

### 命令与结果

```text
python -m pytest agent_service/tests -q
  → 210 passed, 20 subtests passed

go build ./...
  → exit 0
go test ./handlers/... ./store/... ./internal/...
  → ok  handlers / ok  store / ok  internal/agentproxy
```

> `go test ./...` 中 `topicsearch` 有 2 个既有失败
> （`TestSearchRunsStructuredPipelineAndDeduplicatesResults`、
> `TestSearchWithLocalToolRunsDeepSeekToolLoop`）。已用 `git stash` 在 Phase 9
> 之前的提交上复现同样失败，属既有缺陷、与 Agent 升级无关，按 61.0.5 未顺手改动。

---

## 2. Task 9.1 覆盖盘点（7 项要求逐条对应）

| 要求 | 现有测试 | 结论 |
| --- | --- | --- |
| candidate display mapping | `test_task_store.py`（原始 A/B/C 展示 B/C/A → 第三个解析为 A、display_index 唯一约束）、`test_topic_expert.py`（重启后仍按持久化 mapping 选中）、`test_phase2_e2e.py::TopicCardDisplayMappingTest`（卡片载荷与解析结果同源） | 已覆盖 |
| draft version | `test_drafts.py`（create→v1、edit→v2、v1 仍存在、current=v2、重复 version 被 DB 拒绝、失败编辑不推进指针） | 已覆盖 |
| authorization invalidation | `test_authorization.py`（v2 授权在草稿升到 v3 后失效；授权绑定当前版本与参数） | 已覆盖 |
| generation idempotency | `test_generation_service.py`（同一授权只提交一次）、`test_phase2_e2e.py`（重复生成复用同一 generation）、`test_failure_injection.py`（超时/未知提交不重复调用 provider） | 已覆盖 |
| Research status | `test_recruitment_status.py`（规则表驱动：未来截止=open、无可用时间=unknown、官方页不可达=unknown、可信事实冲突=unknown） | 已覆盖 |
| Review version binding | `test_review_store.py`、`test_review_invalidation.py`、`test_content_reviewer.py`（审稿绑定 current_version；改稿后旧 pass 不再生效） | 已覆盖 |
| Memory thread isolation | `test_cross_thread_memory.py`（两个 thread 的指针与草稿互不干扰） | 已覆盖 |

结论：**核心 domain tests 全绿，无“明显空缺”需要新增测试。**

---

## 3. Task 9.2 并发与串行结论

文档 Case：

```text
current=v1
并发消息 A：第二段短一点
并发消息 B：开头换问句
期望：v1 → v2 → v3，不得产生两个互相覆盖的 v2
```

实测：

- `test_two_quick_edits_land_as_v2_and_v3`：两个改稿请求并发发出，最终版本序列 `[1, 2, 3]`，三个版本的正文互不相同，当前指针 = v3，两个请求分别上报 v2 与 v3。
- `test_serial_edits_still_advance_one_version_at_a_time`：串行改稿仍逐版递增。
- `test_two_connections_reserve_different_versions`：两个连接代表两个 worker，先各自读到 v1，再先后写入，得到 v2 与 v3（不会都尝试 v2）。

为满足该 Case，Task 9.2 内做了两处**必要**修正：

```text
agent_service/services/task_store.py::save_draft_version
  版本号由「先读 current_version 再 +1」改为单条
  UPDATE drafts SET current_version = current_version + 1 ... RETURNING current_version
  先占号再 INSERT：两个写者读到同一版本号时，后者不会再撞 UNIQUE 约束

agent_service/db/database.py::connect_database
  增加 busy_timeout（5s）：第二个写者（多 worker / 同目录第二个进程）等待而不是立刻失败
```

---

## 4. Task 9.3 故障注入结论

`test_failure_injection.py` 覆盖文档列出的 9 类故障：

| 故障 | 用户可见响应 | 业务状态 |
| --- | --- | --- |
| search 0 result | 结论保持 unknown，并给出待确认问题 | 不写入任何 verified 事实 |
| source 404 | 该来源不可用，结论降级 | 事实标为 not_verified，不写成 open |
| search timeout | 可重试的兼容性错误 | 结论 unknown，不伪造判定 |
| Research unknown | 如实回答无法确认 | recruitment_status = unknown，reason 可读 |
| DB commit failure | 明确「保存失败」而不是崩溃 | 当前稿件版本与正文保持不变 |
| provider submit timeout | 提交结果未知（unknown_submission） | 只留一条记录，provider_task_id 为空，重复请求不再调用 provider |
| provider unknown_submission | 同上，保持未知语义 | 同上 |
| Specialist 调用达到上限 | 轮次被上限终止，返回可读原因 | 只有 1 个批次落库，被拒调用没有到达子 Agent |
| recursion_limit | 运行被上层上限截断，不会无限循环 | 未提交任何业务对象（candidate_batches = 0） |

其中最后一类为满足验收新增了一处必要修正：

```text
agent_service/agent_tools/script_expert.py
  稿件落库包在 try/except (sqlite3.Error, ValueError) 内，
  失败返回 DRAFT_SAVE_FAILED 工具结果（与 topic/research/review 三个 wrapper 一致），
  不再让数据库错误升级成整轮 INTERNAL_ERROR
```

---

## 5. Task 9.5 最终 4 个 Case 的验收状态

### Case A｜正常链路 —— **未实测（唯一未实测项，已明确记录）**

七句对话、系统应做的事、逐轮可观察判据全部写入 `docs/demo-script.md`。

**验收状态：未实测。** 本环境没有模型凭证（`.env` 不存在、`SUPERVISOR_MODEL` 为空），
最终决定**不补跑真实模型对话**，因此 Case A 的完整链路在本报告中不声明为已验证。

已经实测到的部分（只到契约边界，不含模型输出）：

```text
已实测：Browser/curl → Go /api/agent/chat/stream → Python Supervisor → 契约化 SSE 帧 → 浏览器渲染
实测帧：{"type":"error","payload":{"error_code":"INTERNAL_ERROR",
        "message":"supervisor model name is missing; set SUPERVISOR_MODEL","retryable":false}}
未实测：七轮对话中由真实模型驱动的 tool 选择与文本
```

即便将来补跑，Case A 的每一轮判据也已在 `docs/demo-script.md` 固定，不需要重新设计。

### Case B｜不确定事实 —— **自动化已覆盖**

- `test_research_agent.py::test_insufficient_evidence_returns_unknown`
- `test_recruitment_status.py`（官方页不可达/无可用时间/事实冲突 → unknown）
- `test_failure_injection.py`（search 0 result、source 404、search timeout 三种路径都不得给出 verified 结论）

### Case C｜恢复 —— **真实进程重启矩阵 PASS**

| 步骤 | 结果 |
| --- | --- |
| 重启前基线 | PASS |
| 重启 Python | PASS |
| 重启 Go | PASS |
| 两者同时重启 | PASS |

四次断言逐字段一致：topic / draft_id / draft 版本 / review_id / generation_id /
provider_task_id / thread 消息数（6）/ 候选卡片数（3）。
重启后再向该 session 发一条真实请求（`刚才那版继续改一下`），Go 返回 200 且报出模型缺失的契约化错误，
随后状态仍逐字段一致 —— 说明重启后的会话可继续使用，且不会因这次请求破坏已提交状态。

完整记录见 `docs/restart-recovery.md`。

### Case D｜幂等 —— **端状态实测 + 自动化全覆盖**

端状态检查脚本 `scripts/check_idempotency.py` 断言：

```text
generation_records 行数 = 1（generation_id = generation_seed_1）
generation_authorizations（authorized）= 1
provider task 文件中 provider_seed_1 的条数 = 1
草稿版本序列 = [1]
```

实测：连续两次 `生成吧` 之后，上述判据仍全部成立（PASS）。
真实 provider 提交路径的幂等由自动化测试覆盖：
`test_generation_service.py`（同一授权只提交一次）、`test_phase2_e2e.py`（重复生成复用同一 generation_id）、
`test_failure_injection.py`（超时/未知提交都不重复调用 provider）。

---

## 6. 交付物清单

```text
docs/demo-script.md              最终演示脚本（Task 9.5 产物 1）
docs/final-acceptance-report.md  本文件（Task 9.5 产物 2）
docs/restart-recovery.md         Task 9.4 重启恢复证据
docs/custom-graph-decision.md    Task 7.1 决策文档（NO_GRAPH_NEEDED）
scripts/seed_phase8_session.py   固定会话夹具（业务库 + checkpointer + provider 任务）
scripts/check_recovery.py        Case C 逐字段检查
scripts/check_idempotency.py     Case D 端状态检查
```

## 7. 已知限制与未做项

```text
1. 【未实测】最终 Case A（真实模型驱动的七轮对话）未运行：本环境无模型凭证，
   验收时明确决定不补跑。Case A 的判据已在 docs/demo-script.md 固定，
   但本报告不声明该链路已验证。
2. 真实数字人提交未执行（无蝉镜凭证）；提交与状态查询的契约由 Go/Python 两侧测试覆盖。
3. topicsearch 2 个既有测试失败，与本次升级无关，未顺手修复。
4. 未做多 worker / 多进程压测：仅验证了两个连接代表两个 worker 的版本占号行为。
5. 未做前端自动化 UI 测试（Phase 8 卡片明令不做“小牛马动画”，也不要求 UI 测试框架）。
```

### 7.1 冻结后追加（用户明确授权，2026-10-01）

```text
需求：把 Agent 需要的配置集成进前端，用户直接填写。

新增：前端「Agent 模型设置」面板（/agent.html 右栏）
      PUT/GET /api/agent/config（Go）
      密钥与蝉镜设置共用同一套机器绑定 AES-256-GCM 加密，存同一个 config.json 的 agent 段
      Python 侧解密读取（mtime 缓存），改完下一轮对话生效，无需重启
      补充依赖：pycryptodome、langchain-deepseek、langchain-openai

实测结论：
- 面板保存 → config.json 中密钥为 ENC: 密文，GET 只回掩码
- Python 解密成功并真正调用 provider：假 Key 得到 DeepSeek 401 Authentication Fails
- 因此「凭证配置」这一环已打通；真实 Key 下的最终 Case A 仍未运行（第 1 条不变）
- 测试：Python 217 passed；Go store/handlers/agentproxy 全绿；
  新增 test_agent_config.py(7)、store/agent_config_test.go(3)、handlers/agent_config_test.go(2)

附带修复（Windows）：handlers 测试夹具用 json.Marshal 转义 local_path，
原实现把 "D:\..." 直接拼进 JSON 导致 config.json 非法（\U 非法转义），
遮蔽了依赖 /api/settings 的用例。
```

## 8. 冻结声明

```text
Phase 0 ～ Phase 9 共 56 个 Task 已全部执行完毕。
Phase 7：NO_GRAPH_NEEDED（不引入 Custom StateGraph）
Phase 8：前端产品化完成（/agent.html）
Phase 9：测试、故障注入与作品集冻结完成

未实测项：最终 Case A（真实模型驱动链路）——明确记录，不补跑。
其余验收均为实测或自动化测试结论。

MVP 到此冻结。不继续“顺便优化”。
```
