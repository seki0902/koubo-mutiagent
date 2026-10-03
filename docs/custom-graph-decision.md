# Task 7.1｜Graph Necessity Review

结论（二选一，无第三项）：

```text
NO_GRAPH_NEEDED
```

Phase 7 到此结束，**SKIP Task 7.2**，下一阶段为 Phase 8。

---

## 1. 决策门要求

Task 7.1 只评估、不编码，根据真实 Trace 判断是否已经出现以下四种信号：

```text
A. Research 多来源并行 + 汇总
B. 稳定的补证据循环
C. 必须 interrupt/resume 的子流程
D. 纯 Agent 路由导致明显不可控的确定性流程
```

判定规则：四种信号都**没有**出现，结论只能是 `NO_GRAPH_NEEDED`；只有其中一种被真实证据证明存在，才能写 `GRAPH_NEEDED_FOR: <具体局部流程>`。不得写“以后可能”。

---

## 2. 证据来源与可读性

### 2.1 真实 Trace 数据

本地业务 Trace（Task 6.5 的 `business_trace_events`，Business SQLite）实测状态：

| 项目 | 实测值 |
| --- | --- |
| 落库事件总数 | 263 |
| 覆盖 request_id | 7（`req-stream`、`req-a`、`req-b`、`req-correlate`、`req-sse`、`req-disconnect`、以及 1 个乱码中文 request_id） |
| 事件类型 | 只有 `agent_started` / `message_chunk` / `message_completed` / `waiting_user` / `agent_finished` / `error` |
| `tool_started` / `tool_finished` | 0 |
| `candidate_batch_updated` / `draft_updated` / `review_updated` / `generation_*` | 0 |
| `research_results` / `research_facts` / `research_sources` 行数 | 0 / 0 / 0 |
| `drafts` / `draft_versions` / `reviews` / `generation_authorizations` / `generation_records` 行数 | 0 |
| Checkpointer DB（`checkpoints.sqlite`） | 该运行目录下不存在 |

结论：**现有 Trace 只覆盖 runbook / 流式验收这类注册事件，没有任何一次完整业务轮（选题 → 写稿 → 审稿 → 授权 → 提交）被真实 Trace 记录过。** 因此本 Task 的判定不能依赖“历史使用频次”，只能依赖 Phase 0~6 已经落地的架构事实与代码路径；这不会削弱结论，因为决定 A~D 是否成立的是这些路径本身，而不是它们被调用过多少次。

### 2.2 被检查的代码路径

```text
agent_service/agents/supervisor.py            Supervisor 主架构与流式/阻塞两条路径
agent_service/schemas/state.py                ContentAgentState（thread 级指针状态）
agent_service/agent_tools/research_expert.py  Research 子流程入口
agent_service/agents/research.py              Research Agent 证据收集与归一化
agent_service/agent_tools/content_reviewer.py 审稿入口与版本绑定
agent_service/agents/review.py                审稿结果与 ready_for_generation 闸门
agent_service/agent_tools/script_expert.py    写稿入口与指针失效
agent_service/runtime/budget.py               63.5 限额常量与计数器
agent_service/runtime/checkpointer.py         AsyncSqliteSaver 线程持久化
agent_service/schemas/events.py               事件契约
agent_service/services/business_trace_store.py 本地业务 Trace
```

同时确认：`agent_service` 全目录检索 `interrupt` / `Command(resume=...)` 无任何命中，即当前系统没有任何 LangGraph 中断点。

---

## 3. 逐项判定

### A. Research 多来源并行 + 汇总 —— 未出现

- Research Agent 只有一个 `search_recruitment` 工具和一个 `fetch_source` 工具，由模型在**同一条 tool-calling 循环**里按需依次调用；`agents/research.py` 里没有并行扇出，也没有任何地方创建并发分支。
- 证据汇总已经是**确定性代码**，不依赖图结构：`_normalize_evidence()` 把模型给出的 `source_id` 映射回真实观测到的证据（按 `source_id` 或 `url` 对齐），证据不足时把 `verification_status` 降级为 `not_verified`；`_source_rank()` 按 official/government/university/employer → platform → 其他固定排序。
- 对外结论由 `RecruitmentStatusService.determine_recruitment_status()` 与 `ResearchAgentService.answer_status()` 给出，都是纯函数式判定，不需要在多分支图上做 join。

判定：**多来源不是并行 fan-out，而是单 Agent 顺序取证 + 确定性汇总。** 信号 A 未出现。

### B. 稳定的补证据循环 —— 已被人为封顶，不属于“需要图来约束”的循环

- 补证据循环确实存在（审稿 `need_research` → 再研究一次 → 再审），但它是**单轮、有上限**的：`MAX_RESEARCH_ROUNDS = 2`，`claim_research_round()` 在超限时返回 `BUDGET_EXCEEDED` 并写明原因，供模型如实转述。
- 外层还有 `MAX_SPECIALIST_CALLS = 6`、`MAX_SAME_SPECIALIST_CALLS = 2`、`MAX_TOOL_RETRIES = 1`、`RECURSION_LIMIT = 12` 四道确定性上限。
- Supervisor 的 system prompt 明确写死“最多再研究一次，仍然缺证据就如实说未知，不要循环研究”。
- 因此“循环会不会失控”已经由应用层计数器回答，不需要用 StateGraph 的边去表达；把它搬进图只会把同一份约束复制到两处。

判定：**信号 B 未出现为“需要图”的理由。** 循环存在但已封顶，且封顶点在业务语义上比图边更准确。

### C. 必须 interrupt/resume 的子流程 —— 未出现

- “用户明确授权后才提交数字人”是全局协议里的硬闸门，但它的实现是**确定性 Python 调用**：`classify_generation_intent()` 判断意图，`AuthorizationService.authorize_generation()` 校验草稿版本与参数并登记授权，`SupervisorService._authorize_and_submit()` 只有在拿到授权后才调用 `GenerationService.submit()`。整条链在**一次** `ainvoke` 内完成，没有任何需要挂起再恢复的中间态。
- 跨轮恢复由线程 checkpointer 承担：`AsyncSqliteSaver` + `thread_config(session_id)`，一个 `session_id` 的指针状态在整轮结束后仍然可被下一次请求读到；`_Turn` 每轮打开并关闭同一份 Business SQLite，中途不需要 `interrupt()`。
- 阻塞路径与流式路径共用同一套工具、同一份轮次记账和同一个提交后处理，二者不会漂移；流式断开由 `chat_stream` 的 finally 把取消传播给 producer 并释放连接，同样不依赖中断点。

判定：**信号 C 未出现。** 需要“人确认”的地方是业务授权闸门，不是需要暂停图执行的子流程。

### D. 纯 Agent 路由导致明显不可控的确定性流程 —— 未出现

- 路由是纯 Agent 的（Supervisor 决定调哪个 specialist），但每一步的**确定性约束都写在工具包装层**，模型无法绕开：
  - `topic_expert`：`topic_id` 由业务层生成，`第 N 个` 走持久化候选批次解析，不允许模型自造主键或拿 Python 列表下标当编号。
  - `script_expert` / `content_reviewer`：审稿版本由草稿已提交的 `current_version` 指针决定，模型不能指定版本；草稿更新会清掉 `active_review_id`，审稿结果通过 `Command(update=...)` 写回指针。
  - `research_expert`：主题解析、证据归一化、招募状态判定全部在包装层完成。
- 状态本身被刻意做小：`schemas/state.py` 的 `serialize_state()` 用白名单拒绝任何越界字段，业务对象（草稿版本、授权记录、生成记录）永不进入 checkpoint，权威真值始终在 Business SQLite。
- 结果是：模型只负责“选哪个专业的活”，确定性流程没有暴露在模型自由决策之下。**未观察到不可控**，因此没有“必须用固定图替换路由”的证据。

判定：**信号 D 未出现。**

---

## 4. 结论与理由汇总

```text
A. Research 多来源并行 + 汇总        → 未出现（顺序取证 + 确定性汇总，已在 research.py）
B. 稳定的补证据循环                  → 未出现（循环存在但被 MAX_RESEARCH_ROUNDS / RECURSION_LIMIT 封顶）
C. 必须 interrupt/resume 的子流程    → 未出现（授权是确定性调用 + checkpointer 跨轮恢复）
D. 纯 Agent 路由导致不可控确定性流程 → 未出现（确定性约束在工具包装层与指针状态）

最终结论：NO_GRAPH_NEEDED
```

不引入 Custom StateGraph 的核心理由：

1. 当前唯一被反复要求的确定性（版本绑定、授权闸门、证据归一化、额度封顶）都已在应用层实现并可独立测试，图结构无法提供额外保证，只会产生第二份真相。
2. 没有并行扇出需要 join，也没有跨请求挂起需要中断点，StateGraph 的两个主要收益在本系统中都没有对应需求。
3. Supervisor 目前的 `create_agent` + `Command` 指针状态 + `AsyncSqliteSaver` 组合已经满足 Phase 2~6 的全部验收，保持现状符合 61.0.5“禁止顺手重构”。

## 5. 触发重新评估的条件

出现以下任一情况时，本决策需要重做（本轮不实现、不预设）：

```text
1. Research 出现真实的并行取证需求（同一次取证要并发多源并在图上做 join / 去重合并）。
2. 补证据循环被真实 Trace 证明会在上限内反复空转，且无法通过调整限额或提示词收敛。
3. 出现必须在图执行中途挂起、由外部事件恢复的子流程（例如外部审核回调），checkpointer 跨轮恢复不够用。
4. 出现一条业务上必须固定顺序、且模型路由已被证明会违反的流程。
```

## 6. Task 7.1 验收

```text
决策文档：docs/custom-graph-decision.md
结论：NO_GRAPH_NEEDED（二选一之一，已明确写出）
越界改动：无（本 Task 只新增该文档）
```

按 Task 7.2 的执行前提，`NO_GRAPH_NEEDED` 时直接 **SKIP Task 7.2，进入 Phase 8**。
