# AI 数字人内容生产 Multi-Agent 系统｜LangChain + LangGraph 开发文档 V6

> **本文件是项目唯一的人类执行说明入口。每个 Task 已内置可直接复制给 Codex / GPT-6 的执行 Prompt。**

> 版本：V6.1（唯一执行入口版）
>
> 本文档基于 V5 技术路线重构版继续细化。V6 不再调整核心技术路线，重点把开发工作拆成可独立执行、独立验收、完成即停止的 Task。
>
> **产品逻辑不变；底层 Agent 技术路线由 OpenAI Agents SDK 调整为 LangChain + LangGraph。**
>
> 前台仍然是一个连续对话框。
>
> 后台仍然采用：
>
> **Supervisor Agent + Topic Agent + Research Agent + Script Agent + Content Review Agent**
>
> 但实现方式调整为：
>
> **LangChain `create_agent` + Subagents-as-tools + LangGraph Checkpointer / State / Interrupt / Streaming + LangSmith Tracing**
>
> 数字人生成仍然属于 Tool，不单独设计为 Agent。
>
> 核心原则保持不变：
>
> **能力预先定义，路径不预先定义。**
>
> **用户只和 Supervisor 对话，Supervisor 自主调用专业 Agent 和 Tool 完成任务。**

> V6 执行重点：
> - 不再把“LangChain”和“LangGraph”理解为二选一；
> - LangChain 负责高层 Agent harness、模型、Tool、结构化输出与 Middleware；
> - LangGraph 负责 thread 级持久化、状态恢复、interrupt、streaming，以及后续必要的确定性 workflow；
> - 第一版不手写完整 `StateGraph`，优先使用 `create_agent`；
> - Supervisor 采用 subagents-as-tools，而不是固定 Topic → Research → Script → Review 路径；
> - 业务对象版本、授权、事务、幂等仍由确定性业务代码保证；
> - 现有 Go 工具继续复用，不因框架切换而重写。
>
> **注意：本文对现有 Go 模块的复用边界仍属于工程建议。正式开发前必须执行 Phase 0 代码审计，以真实函数、路由和数据结构为准。**

---

# 1. 为什么本项目改用 LangChain + LangGraph

V4 使用 OpenAI Agents SDK 的原因，是它非常适合：

```text
Supervisor
+
专业 Agent
+
Tools
+
连续对话
```

但随着工程约束补齐，本项目实际已经需要：

```text
多轮对话
当前任务状态
稿件版本
审核状态失效
生成授权
服务重启恢复
跨会话内容资产
流式状态事件
多模型接入
```

这些需求并不是单纯的“Agent 调 Tool”。

当前 LangChain 的 `create_agent` 本身建立在 LangGraph 之上，因此本项目不需要在下面两个极端之间二选一：

```text
极端 A：只使用轻量 Agent SDK，状态能力大量自己补

极端 B：所有业务都手写成 LangGraph Node / Edge
```

V6 采用中间方案：

```text
LangChain
= Agent 层

LangGraph
= 状态 / 持久化 / 恢复 / HITL / 流式执行底座

LangSmith
= Trace / Debug / Evaluation
```

具体分工：

```text
LangChain create_agent
├── Supervisor Agent
├── Topic Agent
├── Research Agent
├── Script Agent
└── Content Review Agent

LangChain Tools
├── 专业 Agent Tool wrapper
├── Search Tools
├── State / Memory Tools
└── Digital Human Tool

LangGraph
├── Checkpointer
├── Thread State
├── Interrupt / Resume
├── Streaming
└── 必要时 Custom StateGraph

LangSmith
└── Agent / Tool / Model Trace
```

第一版仍然不设计固定流程：

```text
Topic → Research → Script → Review
```

而是：

```text
Supervisor
↓
根据当前用户请求和状态
↓
自主选择调用哪个专业 Agent / Tool
```

只有下面这类**确定性业务约束**交给代码或后续 Custom Graph：

```text
版本递增
状态失效
事务
生成授权
幂等
并发控制
失败恢复
```

因此 V6 的技术定义不是：

```text
把项目“改回 LangGraph”
```

而是：

```text
LangChain Agent Harness
+
LangGraph Stateful Runtime
```

---

# 2. 项目定位

现有网页工具已经具备：

```text
输入关键词
↓
自动搜索选题
↓
人工选择
↓
DeepSeek 生成口播稿
↓
人工修改
↓
数字人生成
```

本期不重新开发这些基础能力。

本期改造目标：

```text
按钮驱动工具
↓
升级为
↓
对话驱动 Multi-Agent 内容生产系统
```

用户以后不需要知道：

```text
选题按钮在哪里
搜索按钮在哪里
写稿按钮在哪里
数字人按钮在哪里
```

用户只需要表达：

```text
今天做啥好呢
这个方向可以，但是换几个还在招聘的
第三个写吧
第二段短一点
开头换成问句
这个版本可以
生成吧
```

系统负责理解当前状态并推进任务。

---

# 3. 最终用户体验

## 3.1 开放式选题

用户：

```text
今天做啥好呢？
```

系统内部：

```text
Supervisor
↓
Topic Agent
↓
Topic Agent 根据需要调用搜索 Tool
↓
必要时 Supervisor 调 Research Agent
↓
Topic Agent / Supervisor 汇总
↓
返回 3～5 个候选选题
```

用户看到：

```text
我看了一下近期热点和仍有时效的信息，
这几个方向比较值得做：

1. ...
2. ...
3. ...

每个方向包含：
内容切口
目标人群
为什么现在值得做
关键信息
原始链接
```

---

## 3.2 用户修改筛选条件

用户：

```text
这个方向可以，但是帮我找其他还在招聘的。
```

系统理解：

```text
保留：
当前内容方向

修改：
企业 / 招聘对象候选

增加条件：
仍然在招聘
```

系统内部可能：

```text
Supervisor
↓
Research Agent
↓
search_recruitment
↓
Research Result
↓
Topic Agent
↓
重新组织候选
↓
Supervisor 回复用户
```

---

## 3.3 写稿

用户：

```text
第三个可以，写吧。
```

系统知道：

```text
“第三个” = 当前 topic_candidates[2]
“写吧” = 基于该选题生成口播稿
```

Supervisor：

```text
调用 Script Agent
```

Script Agent读取：

```text
confirmed_topic
verified research
IP Principles
Script Principles
```

返回稿件。

---

## 3.4 改稿

用户：

```text
第二段太长了，压一下。
```

系统知道：

```text
当前对象 = current_draft
修改范围 = 第二段
修改目标 = 更短
```

Supervisor 调 Script Agent。

Script Agent：

```text
只修改相关部分
保留其他已确认内容
生成新版本
```

---

## 3.5 审核

用户可以直接说：

```text
看看有没有风险。
```

Supervisor 调：

```text
Content Review Agent
```

也可以在准备生成前，由 Supervisor 判断是否需要审核。

---

## 3.6 数字人生成

用户：

```text
生成吧。
```

Supervisor 理解：

```text
用户已授权使用当前稿件版本生成数字人
```

然后调用：

```text
generate_digital_human Tool
```

返回：

```text
生成中
完成
视频结果
```

---

# 4. 系统整体架构

```text
Browser Frontend
        │
        ▼
Go Application / Gateway
        │
        ▼
Python Agent Service
        │
        ▼
LangChain Supervisor Agent (`create_agent`)
        │
        ├── topic_expert Tool
        │     └── Topic Agent (`create_agent`)
        │
        ├── research_expert Tool
        │     └── Research Agent (`create_agent`)
        │
        ├── script_expert Tool
        │     └── Script Agent (`create_agent`)
        │
        ├── content_reviewer Tool
        │     └── Content Review Agent (`create_agent`)
        │
        ├── Function Tools
        └── Digital Human Tool
        │
        ▼
Final Response / Stream Events
```

底层由 LangGraph 提供 stateful runtime：

```text
LangChain create_agent
        │
        └── built on LangGraph
                │
                ├── Checkpointer
                ├── Thread State
                ├── Interrupt / Resume
                ├── Streaming
                └── Custom StateGraph（按需，不是第一版主流程）
```

V6 保留三类状态，但重新明确“谁是业务真相”：

```text
1. Thread State / Checkpointer
= 当前对话历史 + 轻量运行状态 + 当前业务对象指针

2. Business State Store
= 当前任务的版本化业务对象
= Draft / Review / Authorization / Generation 等唯一业务真相

3. Long-term Content Memory
= 跨 thread 长期积累的内容资产、表现、修改记录与账号知识
```

重要：

```text
Checkpointer 不是业务数据库替代品。
```

稿件正文、历史版本、审核结果、生成授权等需要 SQL 约束和审计的数据，仍然写入 Business State Store；Thread State 只保存必要指针和对话上下文。

## 4.1 现有 Go 项目与 Python Agent Service 的工程边界

当前项目主体是 Go。V6 仍不建议为了换 Agent 框架而重写现有 Go 能力。

```text
Browser Frontend
      │
      ▼
Go Application / Gateway
- 继续承载现有网页与静态资源
- 保留现有搜索、写稿、数字人等已验证能力
- 对外维持同源 API
- 代理 /api/agent/* 到 Python Agent Service
      │
      ├──────────────► Python Agent Service
      │                - LangChain Supervisor / Specialist Agents
      │                - LangGraph Checkpointer / Thread State
      │                - Business State / Draft Version
      │                - Generation Authorization
      │                - Long-term Content Memory
      │                - LangSmith Trace / Event mapping
      │
      ◄───────────────┤
      │   Internal Tool API
      ▼
Existing Go Capabilities
- Search
- Existing DeepSeek service（如继续复用）
- Digital Human API adapter
```

核心边界：

```text
Python = Agent 编排 + thread runtime + 业务状态所有者
Go = 现有能力提供者 + Web/Gateway
```

正式开发前先检查现有 Go 代码。如果某项能力已有 HTTP API，直接复用；如果只是 Go 内部函数，则优先增加最薄的 internal adapter，而不是复制一份 Python 实现。

### 4.1.1 第一版数据归属

| 数据 | 主写入方 | 技术实现 | 说明 |
|---|---|---|---|
| 对话历史 / Thread State | Python Agent Service | LangGraph Checkpointer | 通过 `thread_id` 恢复 |
| 当前业务对象指针 | Python Agent Service | Agent State + Checkpointer | 例如 current_draft_id，不保存完整历史真相 |
| ContentTask / Draft Version | Python Agent Service | SQLite Business DB | 当前任务唯一业务真相 |
| 候选批次 / 展示顺序 | Python Agent Service | SQLite Business DB | 保证“第三个”准确解析 |
| Review Result | Python Agent Service | SQLite Business DB | 必须绑定 draft_id + version |
| Generation Authorization | Python Agent Service | SQLite Business DB | 必须绑定具体稿件版本与参数 |
| Generation Record | Python Agent Service | SQLite Business DB | 保存 idempotency_key、provider_task_id、状态 |
| 搜索原始结果 | Go capability → Python | Internal API | Go 只返回数据，不替 Agent 做内容判断 |
| 数字人供应商任务 | Go capability / 外部 API | Internal API | Python 保存业务映射 |
| Long-term Content Memory | Python Agent Service | 第一版 SQLite 结构化表；后续可接 LangGraph Store | 跨 thread 使用 |

### 4.1.2 Checkpointer 选择

第一版本地 / 作品集部署采用：

```text
langgraph-checkpoint-sqlite
+
AsyncSqliteSaver
```

用途：

```text
保存 thread checkpoint
恢复多轮对话
进程重启后恢复运行状态
支持 interrupt / resume
```

不使用 `InMemorySaver` 作为正式演示数据源，因为进程重启后会丢失状态。

未来如果改为多实例 / 正式服务，再迁移：

```text
PostgresSaver
```

### 4.1.3 Long-term Memory 选择

LangGraph 原生区分：

```text
Checkpointer
= thread-scoped short-term memory

Store
= cross-thread long-term memory
```

但本项目的长期 Memory 第一版本质上是高度结构化的业务资产：

```text
content_items
script_versions
revision_history
content_metrics
topic_history
```

因此第一版仍优先使用 SQLite 关系表 + Memory Tools，避免为了“框架统一”把业务表强行塞入通用 Store。

后续如果需要：

```text
跨 thread 通用记忆
语义检索
用户级 memory namespace
```

再增加 LangGraph Store / PostgresStore。

### 4.1.4 对外与内部接口

浏览器仍只访问 Go 服务：

```text
Browser
  ↓
Go public API
  ↓ proxy
Python Agent API
  ↓
Go internal capability API
```

建议接口：

```text
# Browser → Go（public）
POST /api/agent/chat
GET  /api/agent/tasks/{session_id}
GET  /api/agent/events/{session_id}
GET  /api/agent/generation/{generation_id}

# Go → Python（internal / loopback）
POST /internal/agent/chat
GET  /internal/agent/tasks/{session_id}
GET  /internal/agent/events/{session_id}

# Python → Go（internal capability）
POST /internal/tools/search-platform
POST /internal/tools/search-recruitment
POST /internal/tools/fetch-source
POST /internal/tools/generate-digital-human
GET  /internal/tools/generation/{provider_task_id}
```

如果 Phase 0 发现现有模块已有稳定 HTTP 接口，则直接适配，不强制使用上述路径命名。

### 4.1.5 启动与配置

开发环境建议：

```text
Go service       : localhost:8080
Python Agent     : localhost:8090
Business SQLite  : agent_service/db/app.sqlite
Checkpoint SQLite: agent_service/db/checkpoints.sqlite
```

环境变量示意：

```text
AGENT_SERVICE_BASE_URL=http://127.0.0.1:8090
GO_CAPABILITY_BASE_URL=http://127.0.0.1:8080
LANGSMITH_TRACING=true   # 可选
```

第一版不要求容器化。

---

## 4.2 Agent 自主性与业务硬约束的分界

本项目继续坚持：

```text
能力预先定义，路径不预先定义。
```

但这只适用于“下一步做什么”。以下事项不能交给 LLM 自由决定：

```text
对象 ID
候选展示顺序
稿件版本号
状态失效规则
数据库写入事务
并发控制
审核结果对应版本
生成授权对应版本
数字人提交幂等
超时与重试
```

因此：

```text
Agent
= 决定是否搜索、是否调研、是否写稿、是否审核、如何回答

LangGraph Runtime
= 保证 thread 可恢复、可中断、可流式执行

Business Code
= 保证动作只作用于正确业务对象，并以可追溯方式落库
```

这三层不能混在一起。

---

# 5. 为什么采用 Subagents-as-tools

LangChain 当前的 Multi-Agent 模式中，最符合本项目的是：

```text
Subagents
```

语义是：

```text
一个主 Agent / Supervisor
↓
把专业 Agent 包装成 Tool
↓
Supervisor 决定什么时候调用谁
↓
专业 Agent 返回结果给 Supervisor
↓
Supervisor 继续控制用户对话
```

这与 V4 的 Manager Pattern 产品逻辑完全一致，只是实现从 `agent.as_tool()` 改为 LangChain Tool wrapper。

示意：

```python
from langchain.agents import create_agent
from langchain.tools import tool


topic_agent = create_agent(
    model=topic_model,
    tools=[search_platform_content, search_recruitment],
    system_prompt=topic_prompt,
    response_format=TopicResult,
)


@tool("topic_expert", description="寻找、比较和调整内容选题时调用")
async def topic_expert(task: str) -> str:
    result = await topic_agent.ainvoke({
        "messages": [{"role": "user", "content": task}]
    })
    return result["structured_response"].model_dump_json()
```

Supervisor：

```python
supervisor = create_agent(
    model=supervisor_model,
    tools=[
        topic_expert,
        research_expert,
        script_expert,
        content_reviewer,
        generate_digital_human,
    ],
    system_prompt=supervisor_prompt,
    state_schema=ContentAgentState,
    checkpointer=checkpointer,
)
```

专业 Agent 默认采用**隔离上下文**：

```text
Supervisor 保留完整 thread 对话历史

Specialist 每次收到：
- 清晰任务描述
- 必要结构化业务上下文
- 必要 State / Memory Tool
```

而不是默认把整个聊天历史都塞给 Specialist。

这样可以：

```text
降低上下文污染
减少 token
避免 Specialist 靠猜聊天历史识别当前 draft
强化结构化业务对象
```

如果某个 Specialist 确实需要上游完整消息，可在 wrapper 中显式从 `ToolRuntime.state` 提取并裁剪后传入。

---

# 6. 为什么第一版不以 Handoff 为主

本项目不希望专业 Agent 直接接管用户会话。

用户体验应该始终是：

```text
用户
↕
Content Supervisor
```

而不是：

```text
用户和 Supervisor 聊
↓
Research Agent 接管
↓
Script Agent 再接管
```

因此第一版仍采用：

```text
Supervisor + Subagents-as-tools
```

Handoff 只在未来出现下面需求时考虑：

```text
用户主动进入“专门写稿模式”
用户直接与某专业 Agent 长时间会话
某 Agent 必须持续保有直接用户交互状态
```

当前没有必要。

---

# 7. 五个 Agent

---

# 7.1 Supervisor Agent

Supervisor 是前台唯一对话主体。

名称建议：

```text
Content Production Supervisor
```

它负责：

```text
理解用户最新输入
理解当前对话历史
理解当前任务 State
理解“这个”“第三个”“写吧”“生成吧”等上下文指代

决定：
是否需要 Topic Agent
是否需要 Research Agent
是否需要 Script Agent
是否需要 Content Review Agent
是否需要直接调用 Tool
是否应该回复用户等待确认
是否应该继续内部调查
```

Supervisor 不负责：

```text
自己深入搜索
自己完成完整选题分析
自己完整写稿
自己做专业审核
```

它负责：

```text
统筹
```

---

# 7.2 Topic Agent

Topic Agent 是选题专家。

名称建议：

```text
Topic Strategy Agent
```

职责：

```text
理解账号定位
理解当前内容目标
判断哪些选题值得做
筛选候选方向
设计内容切入角度
评估时效性
避免重复内容
```

它可以拥有：

```text
search_platform_content Tool
search_recruitment Tool
get_content_history Tool
get_ip_profile Tool
```

它读取：

```text
Topic Principles
```

Topic Agent回答：

```text
现在最值得做什么？
```

---

# 7.3 Research Agent

名称建议：

```text
Research & Verification Agent
```

职责：

```text
搜索事实
核实信息
判断来源质量
确认招聘状态
确认时间
确认目标人群
确认企业
确认岗位
找到原始来源
补充 Script 所需资料
```

它可以拥有：

```text
web_search
search_recruitment
fetch_source
get_content_history
```

Research Agent回答：

```text
这个方向有哪些当前、可靠、可以使用的信息？
```

---

# 7.4 Script Agent

名称建议：

```text
Short-form Script Agent
```

职责：

```text
根据 confirmed_topic 写口播稿
结合 Research Result
遵守 IP 人设
遵守 Script Principles
处理用户多轮修改
维护内容结构
控制 Hook
控制节奏
控制广告感
```

Script Agent回答：

```text
这条内容应该怎么讲？
```

---

# 7.5 Content Review Agent

名称建议：

```text
Content Review Agent
```

职责：

```text
事实检查
内容质量检查
IP 一致性检查
平台风险检查
Hook 检查
结构检查
目标人群检查
夸张承诺检查
CTA 检查
```

它回答：

```text
这版内容是否可以进入生成阶段？
```

---

# 8. Supervisor 如何调用专业 Agent

V6 使用“Tool per agent”模式。

每个专业 Agent：

```text
create_agent(...)
↓
由一个 @tool wrapper 暴露给 Supervisor
```

示意：

```python
@tool(
    "research_expert",
    description="当需要搜索、核实、补充最新事实或招聘信息时调用"
)
async def research_expert(
    task: str,
    runtime: ToolRuntime[ContentAppContext, ContentAgentState],
) -> str:
    state = runtime.state
    research_input = build_research_input(task, state)

    result = await research_agent.ainvoke({
        "messages": [{"role": "user", "content": research_input}]
    })

    return result["structured_response"].model_dump_json()
```

Tool Description 仍然只是：

```text
能力说明 + 什么时候适合调用
```

不是固定路由规则。

Supervisor 自己判断：

```text
是否调用
调用哪个
是否连续调用多个
是否需要再次调用同一个 Specialist
什么时候直接回复用户
```

例如：

```text
“第三个写吧”
→ Script

“第三个现在还招吗？”
→ Research

“这个稿有没有风险？”
→ Review

“今天做啥？”
→ Topic，必要时再 Research
```

---

# 9. 专业 Agent 也可以使用普通 Tool

Multi-Agent 不代表：

```text
所有能力都必须做成 Agent。
```

应该区分：

```text
需要复杂专业判断
→ Agent

只负责查询 / 保存 / 调 API
→ Function Tool
```

例如：

```text
搜索
→ Tool

招聘查询
→ Tool

读取稿件
→ Tool

保存稿件版本
→ Tool

数字人 API
→ Tool
```

---

# 10. Function Tools

LangChain 可以通过 `@tool` 将 Python 函数暴露给 Agent。

建议封装现有能力：

```text
search_platform_content
search_recruitment
fetch_source
get_content_history
get_ip_profile
get_current_task_state
save_topic_selection
save_draft_version
get_current_draft
generate_digital_human
get_generation_status

### Long-term Content Memory
search_content_history
get_recent_topics
get_high_performing_content
get_similar_scripts
get_revision_history
save_content_item
save_revision_record
update_content_metrics
```

---

# 11. Tool 设计原则

Tool 负责：

```text
查
读
写
执行 API
```

Agent 负责：

```text
判断
分析
决策
生成
```

例如：

错误：

```text
search_recruitment 返回：
“推荐做这个选题。”
```

正确：

```text
search_recruitment 返回：
企业
招聘对象
截止时间
状态
来源
URL
```

“值不值得做”由 Topic Agent 判断。

---

# 12. Thread / Short-term Memory

V6 不再使用 OpenAI Agents SDK Session，而使用 LangGraph Checkpointer 管理 thread 级短期记忆。

用户连续说：

```text
这个
第三个
写吧
再换几个
刚才那版
第二段
就这样
生成吧
```

系统必须在同一个：

```text
thread_id
```

中连续执行。

第一版本地采用：

```text
AsyncSqliteSaver
```

示意：

```python
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langchain.agents import create_agent

async with AsyncSqliteSaver.from_conn_string(
    "agent_service/db/checkpoints.sqlite"
) as checkpointer:
    supervisor = create_agent(
        model=supervisor_model,
        tools=supervisor_tools,
        system_prompt=supervisor_prompt,
        state_schema=ContentAgentState,
        checkpointer=checkpointer,
    )

    config = {
        "configurable": {"thread_id": session_id},
        "recursion_limit": 12,
    }

    result = await supervisor.ainvoke(
        {"messages": [{"role": "user", "content": user_message}]},
        config=config,
        context=app_context,
    )
```

其中：

```text
前端 session_id
≈
LangGraph thread_id
```

第一版可以保持同值，减少映射复杂度。

专业 Agent 默认不拥有独立持久会话：

```text
Supervisor thread = stateful
Specialist invocation = isolated / stateless by default
```

这是刻意设计，不是缺失。

Specialist 所需的业务对象通过：

```text
ToolRuntime.state
+
Business State Tools
+
Long-term Memory Tools
```

显式获得。

---

# 13. Thread State 和 Business State 不是一回事

这是 V6 必须严格区分的一点。

LangGraph Thread State 主要保存：

```text
messages
当前 task_id
当前候选 batch 指针
current_draft_id / version 指针
必要运行状态
```

Business State Store 保存：

```text
ContentTask
TopicCandidateBatch
Draft / DraftVersion
ResearchResult
ReviewResult
GenerationAuthorization
GenerationRecord
```

所以：

```text
Thread State
= “这一轮对话现在指向哪里”

Business State
= “这些业务对象真实是什么”
```

不能只靠 checkpoint 里的自然语言消息管理业务对象，也不建议把全部稿件版本历史复制进 checkpoint。

推荐原则：

```text
Checkpoint 保存 pointer / summary
SQLite Business DB 保存 canonical object
```

这样既得到 LangGraph 的恢复能力，又保留数据库事务、唯一约束、版本号和审计能力。

---

# 13.5 Long-term Content Memory / Content Database

本项目除了 Thread State 与当前任务 Business State，还必须存在第三类长期状态：

```text
Long-term Content Memory
=
持续积累的内容资产、历史表现、用户修改习惯与账号知识
```

三者职责必须严格区分：

```text
Thread Memory
= 当前对话历史

Task State
= 当前这条内容任务的结构化状态

Long-term Content Memory
= 跨会话长期积累、持续更新的内容资产与经验
```

---

## 13.5.1 为什么需要 Long-term Content Memory

如果系统只有 Thread State 和 Task State，它只能做到：

```text
记得这次对话聊了什么
知道这条内容做到哪一步
```

但它无法真正理解：

```text
这个账号过去做过什么
哪些选题已经重复
哪些稿件历史表现好
哪些 Hook 长期有效
哪些表达用户经常删掉
哪些结构更符合当前 IP
哪些内容最近已经连续做过
哪些稿件最终真正被采用
```

因此，Content Agent 系统必须持续积累长期内容数据。

---

## 13.5.2 Long-term Content Memory 的核心数据

第一版建议至少保存以下几类数据。

### A. 历史内容

```text
content_items
```

建议字段：

```text
content_id
account_id
topic
title
script
content_type
platform
publish_date
status
tags
source_urls
hook
cta
created_at
updated_at
```

---

### B. 发布后表现数据

```text
content_metrics
```

建议字段：

```text
content_id
platform
impressions
views
likes
comments
shares
favorites
clicks
leads
valid_leads
ctr
conversion_rate
lead_rate
collected_at
```

具体字段以现有业务能拿到的数据为准。

---

### C. 稿件版本

```text
script_versions
```

建议字段：

```text
draft_id
content_id
version
script
change_type
user_feedback
is_final
created_at
```

---

### D. 选题历史

```text
topic_history
```

建议字段：

```text
topic_id
topic
content_angle
source
selected
used
publish_date
performance_summary
tags
created_at
```

---

### E. 用户修改记录

```text
revision_history
```

建议字段：

```text
draft_id
version_before
version_after
user_instruction
changed_sections
change_summary
accepted
created_at
```

这部分非常重要。

因为它可以逐渐沉淀：

```text
用户经常要求什么
哪些表达经常被删
什么 Hook 用户偏好
什么结构经常返工
```

---

### F. 内容经验沉淀

```text
content_learnings
```

建议字段：

```text
learning_id
learning_type
description
source_content_ids
confidence
scope
created_at
updated_at
```

例如：

```text
“海归招聘类视频中，问句 Hook 的平均表现优于直接陈述式开头。”
```

第一版不要求系统自动生成复杂经验。

本期所谓 Long-term Content Memory，指“可查询、可更新的内容资产库”，不是让系统在后台自动形成不可解释的人格记忆。

第一版范围：

```text
历史选题
最终稿
版本记录
用户明确修改反馈
发布表现
少量人工/规则确认的内容经验
```

本期不做：

```text
自动抽象长期人格
自动无限归纳用户偏好
向量数据库作为强依赖
无人监督的自学习闭环
```

可以先：

```text
人工写入
+
后续从历史数据中逐步沉淀
```

---

## 13.5.3 哪些 Agent 读取 Long-term Content Memory

### Topic Agent

可以查询：

```text
最近做过哪些选题
哪些主题已经重复
历史高表现选题
相似内容表现
近期账号内容分布
```

用于：

```text
避免重复
寻找高潜方向
保持账号内容节奏
```

---

### Research Agent

可以查询：

```text
过去同类招聘信息
历史来源
过去已核实企业
已有资料
```

用于：

```text
减少重复调查
复用可靠来源
发现旧信息是否已经失效
```

---

### Script Agent

可以查询：

```text
类似选题历史稿件
历史高表现脚本
常用 Hook
用户修改历史
最终采用版本
IP 长期表达习惯
```

用于：

```text
让新稿件越来越贴合账号
减少重复修改
复用有效结构
```

---

### Content Review Agent

可以查询：

```text
历史审核问题
用户常见修改点
账号长期表达风格
历史高风险表达
最终采用版本
```

用于：

```text
提高审核一致性
减少重复出现同类问题
```

---

### Supervisor

第一版不直接读取大量 Long-term Content Memory。

Supervisor 只需知道：

```text
当前是否存在可用长期记忆
专业 Agent 是否已经调用长期记忆 Tool
当前任务是否需要更多历史信息
```

长期内容数据应由专业 Agent 按需查询。

---

## 13.5.4 Long-term Content Memory Tools

建议增加以下 Function Tools：

```text
search_content_history
get_recent_topics
get_high_performing_content
get_similar_scripts
get_revision_history
get_ip_content_profile
save_content_item
save_revision_record
save_publish_metrics
update_content_metrics
save_content_learning
```

---

## 13.5.5 search_content_history

用途：

```text
按关键词 / 标签 / 时间 / 内容类型查询历史内容
```

典型调用：

```text
Topic Agent：
“过去 30 天做过哪些海归金融秋招内容？”
```

---

## 13.5.6 get_recent_topics

用途：

```text
查看近期已经生产或发布的选题
```

用于：

```text
避免重复
判断内容密度
保持选题多样性
```

---

## 13.5.7 get_high_performing_content

用途：

```text
查询历史表现较好的内容
```

可以按照：

```text
平台
内容类型
主题
时间范围
指标
```

过滤。

第一版不要求 Tool 自己定义“什么叫好内容”。

Tool 只返回：

```text
历史数据
```

“是否值得借鉴”由 Agent 根据 Principles 判断。

---

## 13.5.8 get_similar_scripts

用途：

```text
根据当前选题查询相似历史稿件
```

返回：

```text
历史稿件
最终版本
表现数据
标签
```

---

## 13.5.9 get_revision_history

用途：

```text
查询用户过去如何修改类似稿件
```

例如：

```text
开头过长
广告感太强
信息密度不够
表达太正式
CTA 太硬
```

Script Agent 可以借此减少重复犯错。

---

## 13.5.10 save_content_item

当一条内容真正被确认使用时：

```text
保存最终内容资产
```

不要在每次草稿生成时都直接写入长期内容库。

长期库应优先保存：

```text
被选中的选题
确认稿
正式生成内容
最终发布内容
```

草稿版本单独进入 script_versions。

---

## 13.5.11 save_revision_record

每次用户对稿件作出明确修改时：

```text
记录修改前
修改后
用户要求
是否接受
```

这部分是长期个性化的重要来源。

---

## 13.5.12 update_content_metrics

发布后，如果可以获取内容数据：

```text
持续更新表现
```

例如：

```text
发布后 1 天
发布后 3 天
发布后 7 天
```

具体更新频率由现有数据能力决定。

第一版可以人工触发或定时更新。

---

## 13.5.13 长期 Memory 的写入原则

不是所有 Agent 输出都写进长期 Memory。

长期 Memory 只保存：

```text
真正具有复用价值的稳定信息
```

优先写入：

```text
用户最终确认选题
最终采用稿件
真实发布内容
真实表现数据
明确用户修改反馈
经过验证的长期内容经验
```

不优先写入：

```text
临时候选
未采用稿件
模型猜测
未验证分析
一次性中间推理
```

---

## 13.5.14 Long-term Memory 与 Principles 的区别

二者不能混在一起。

```text
Principles
= 业务判断方法

Long-term Content Memory
= 历史事实和经验数据
```

例如：

```text
Principle：
“选题不能只看热度，还要考虑账号人群和时效。”

Memory：
“过去 30 天已经做过 4 条金融秋招内容。”
```

Agent 结合两者做判断。

---

## 13.5.15 第一版技术实现

V6 第一版使用两个 SQLite 文件，职责分开：

```text
checkpoints.sqlite
= LangGraph Checkpointer
= thread message history / Agent State / interrupt resume

app.sqlite
= Business State + Long-term Content Memory
= content_tasks / drafts / reviews / authorizations / content history / metrics
```

不要把 LangGraph checkpoint 表当成业务表直接查询或修改。

`app.sqlite` 建议至少包含：

```text
content_tasks
candidate_batches
candidate_batch_items
research_results
research_facts
research_sources
drafts
draft_versions
reviews
generation_authorizations
generation_records

topic_history
content_items
revision_history
content_metrics
content_learnings
```

而 `checkpoints.sqlite` 由 LangGraph Checkpointer 自己管理。

第一版这样做的原因：

```text
Thread Runtime
与
业务对象 / 长期内容资产
生命周期不同
```

未来如果切换到 Postgres，也仍然保持逻辑分层：

```text
Checkpointer tables
≠
Business tables
≠
Long-term content tables
```

---

## 13.5.16 第一版不需要向量数据库

第一版检索需求主要是：

```text
时间
关键词
标签
主题
账号
内容类型
历史表现
```

SQLite + 普通结构化查询即可。

如果后续需要：

```text
“找语义上和这条稿子最像的历史内容”
```

再考虑：

```text
embedding / vector search
```

第一版不作为必要依赖。

---

## 13.5.17 Memory 的更新闭环

完整闭环：

```text
Agent 读取长期内容库
↓
生成 / 修改新内容
↓
用户确认
↓
数字人生成
↓
内容发布
↓
回收表现数据
↓
写回长期内容库
↓
下一次 Agent 再读取
```

这使系统从：

```text
“每次重新写一条内容”
```

逐渐变成：

```text
“越来越了解这个账号的内容生产 Agent”
```

---

## 13.5.18 Memory MVP 验收

第一版至少做到：

```text
Topic Agent 可以查最近选题
Script Agent 可以查历史相似稿件
Script Agent 可以查用户修改历史
最终稿可以写入长期内容库
发布表现可以后续更新
```

不用第一版就实现复杂自动学习。


# 14. Runtime Context

LangChain / LangGraph 中的 runtime context 用于向 Tool 和 Middleware 传入**不应作为模型可编辑状态**的应用依赖。

建议定义：

```python
from dataclasses import dataclass

@dataclass
class ContentAppContext:
    session_id: str
    user_id: str | None
    task_store: ContentTaskStore
    memory_store: ContentMemoryStore
    search_service: SearchService
    digital_human_service: DigitalHumanService
```

创建 Supervisor 时声明：

```python
supervisor = create_agent(
    model=supervisor_model,
    tools=supervisor_tools,
    context_schema=ContentAppContext,
    state_schema=ContentAgentState,
    checkpointer=checkpointer,
)
```

Tool 通过：

```python
ToolRuntime[ContentAppContext, ContentAgentState]
```

读取：

```text
runtime.context
runtime.state
```

Context 与 State 的区别：

```text
Context
= 服务依赖、当前调用环境
= 不由模型写入 checkpoint

State
= thread 内需要持续保存的运行状态
= 可被 checkpointer 持久化
```

Context 也不等于模型 Prompt。需要让模型看见的信息，应通过 dynamic prompt、tool result 或结构化输入显式提供。

---

# 15. ContentAgentState 与 ContentTaskState

V6 明确存在两种 State，不允许混为一个对象。

## 15.1 ContentAgentState：给 LangGraph Thread 使用

`create_agent(state_schema=...)` 使用轻量 Agent State。

示意：

```python
from langchain.agents import AgentState

class ContentAgentState(AgentState):
    task_id: str | None
    state_revision: int | None

    active_candidate_batch_id: str | None
    selected_topic_id: str | None

    current_draft_id: str | None
    current_draft_version: int | None

    active_review_id: str | None
    generation_authorization_id: str | None
    generation_id: str | None
```

它的职责是：

```text
让 Supervisor 在同一个 thread 中知道
“当前指向哪个业务对象”
```

不建议把下面内容全部复制进 checkpoint：

```text
完整候选列表
全部 Research facts
全部 Draft 历史正文
全部 Review 历史
全部长期内容 Memory
```

否则 checkpoint 会越来越大，并形成重复数据源。

---

## 15.2 ContentTaskState：Business DB 中的业务聚合状态

Business DB 中保留完整、带版本的任务状态：

```python
class ContentTaskState(BaseModel):
    session_id: str
    task_id: str
    state_revision: int = 0

    current_goal: str | None = None
    current_stage: str | None = None

    # Topic pointers
    active_candidate_batch_id: str | None = None
    selected_topic_id: str | None = None
    confirmed_topic_id: str | None = None

    # Research pointer
    research_result_id: str | None = None

    # Draft pointer
    current_draft_id: str | None = None
    current_draft_version: int | None = None
    approved_draft_version: int | None = None

    # Review pointer
    active_review_id: str | None = None
    reviewed_draft_id: str | None = None
    reviewed_draft_version: int | None = None

    # Generation authorization pointer
    generation_authorization_id: str | None = None
    authorized_draft_id: str | None = None
    authorized_draft_version: int | None = None
    authorized_params_hash: str | None = None

    # Generation pointer
    generation_id: str | None = None
    provider_task_id: str | None = None
    generation_status: str | None = None
    generated_video_url: str | None = None

    updated_at: datetime
```

完整对象内容分表保存，例如：

```text
candidate_batches / items
research_facts / sources
draft_versions
reviews
generation_authorizations
generation_records
```

`ContentTaskState` 主要保存当前 pointer，而不是再次复制每张表的所有内容。

---

## 15.3 两者同步规则

唯一真相源：

```text
Business DB
```

写入顺序：

```text
1. Tool / Service 开启 SQLite transaction
2. 校验 expected_state_revision
3. 写业务对象
4. 更新 ContentTaskState pointer
5. commit
6. 返回 Command / Tool result 更新 ContentAgentState pointer
7. LangGraph Checkpointer 保存新的 thread state
```

如果步骤 1～5 失败：

```text
不更新 ContentAgentState
不告诉用户“已保存”
```

如果业务 DB 已成功，但后续 checkpoint 写入异常：

```text
Business DB 仍是事实真相
下一次运行从 task_store 重新 hydrate pointer
```

这可以避免 checkpoint 与业务数据库互相覆盖。

`state_revision` 继续用于防止陈旧结果覆盖新状态。所有写操作都必须携带 `expected_state_revision` 并在事务中校验、递增。

---

# 16. Task Store

第一版建议：

```text
SQLite
```

理由：

```text
本地部署
实现简单
可持久化
便于作品集演示
重启后状态仍存在
```

不需要第一版上：

```text
PostgreSQL
Redis
向量数据库
```

---

# 17. Agent 如何读取业务 State

V6 分三层读取。

## 17.1 Supervisor 读取轻量 Thread State

Supervisor 每轮天然拥有：

```text
messages
+
ContentAgentState 中的轻量业务 pointer
```

例如：

```text
task_id
action_candidate_batch_id
current_draft_id
current_draft_version
active_review_id
generation_id
```

这些字段可以用于理解：

```text
“这个”
“第三个”
“刚才那版”
“生成吧”
```

## 17.2 Tool 读取详细业务对象

详细稿件、候选、审核、授权等仍通过业务 Tool / Store 获取：

```text
get_current_task_state
get_candidate_batch
get_draft_version
get_review_result
get_generation_authorization
```

Tool 内部从 `ToolRuntime.state` 获取 pointer，再从 SQLite Business DB 读取 canonical object。

## 17.3 Specialist 只获得必要上下文

专业 Agent wrapper 负责把：

```text
用户任务
+
必要业务对象
+
必要历史内容
```

整理为 Specialist 输入。

不要默认把 Supervisor 全量聊天历史传给每个 Specialist。

示例：

```text
Research Agent 需要：confirmed topic + 待核实问题
Script Agent 需要：confirmed topic + verified facts + current draft（改稿时）
Review Agent 需要：明确 draft_id + version + sources
```

---

# 18. Structured Outputs

专业 Agent 输出继续强制结构化，但 V6 使用 LangChain `response_format`。

推荐：

```python
agent = create_agent(
    model=model,
    tools=tools,
    response_format=TopicResult,
)
```

最终结构化结果从：

```text
result["structured_response"]
```

读取。

Schema 继续使用 Pydantic。

LangChain 会根据模型能力选择 provider-native structured output 或 tool strategy；因此切换模型时，必须分别验证：

```text
tool calling
structured output
streaming
```

不能因为某个 Provider “兼容 OpenAI API”就默认所有能力一致。

---

# 19. Topic Agent Schema

“第三个”必须绑定用户实际看到的候选顺序，而不是数据库数组当前顺序。

```python
class TopicCandidate(BaseModel):
    topic_id: str
    display_index: int          # 1-based，当前 batch 内展示序号
    title: str
    why_now: str
    target_audience: str
    content_angle: str
    key_points: list[str]
    sources: list[str]
    freshness: str
    risks: list[str]

class TopicResult(BaseModel):
    candidate_batch_id: str
    summary: str
    topic_candidates: list[TopicCandidate]
    research_needed: list[str]
    uncertainties: list[str]
```

规则：

```text
candidate_batch_id + display_index → 唯一 topic_id
```

前端候选卡片必须直接由这个结构化结果渲染。Supervisor 可以补充自然语言说明，但不能在未生成新 batch 的情况下私自重排卡片顺序。

如果 Supervisor 确实需要重排，则必须保存新的 `candidate_batch_id` 和新的 `display_index` 映射。

---

# 20. Research Agent Schema

Research 必须做到“事实—来源”可追溯，而不是把 `facts` 与 `sources` 分成两个无法对应的列表。

```python
class SourceItem(BaseModel):
    source_id: str
    title: str
    url: str
    source_type: str
    published_at: datetime | None
    fetched_at: datetime | None

class ResearchFact(BaseModel):
    fact_id: str
    claim: str
    source_ids: list[str]
    verification_status: Literal[
        "verified", "partially_verified", "not_verified", "conflicting"
    ]
    verified_at: datetime | None
    valid_until: datetime | None
    recruitment_status: Literal[
        "open", "closed", "unknown", "not_applicable"
    ] = "not_applicable"
    notes: str | None = None

class ResearchResult(BaseModel):
    research_result_id: str
    summary: str
    facts: list[ResearchFact]
    sources: list[SourceItem]
    open_questions: list[str]
```

对于招聘信息，至少保存：

```text
核实时间
招聘状态
截止时间 / valid_until（能确认时）
支持该事实的 source_id
无法确认时明确 unknown / not_verified
```

Research Agent 不得把“没有找到关闭信息”等价为“仍在招聘”。

---

# 21. Script Agent Schema

稿件只能有一个主数据来源，避免 `script / hook / body / cta` 各自可写导致不同步。

第一版建议以结构化 `sections` 为主数据，完整稿件由服务层拼接生成：

```python
class DraftSection(BaseModel):
    section_id: str
    section_type: Literal["hook", "body", "cta", "other"]
    order: int
    text: str

class Draft(BaseModel):
    draft_id: str
    version: int
    topic_id: str
    research_result_id: str | None
    sections: list[DraftSection]
    sources_used: list[str]
    uncertainties: list[str]
    created_at: datetime
```

```text
full_script
= 按 order 拼接 sections 后的派生值
```

`full_script` 可在 API 输出时计算，但不作为第二份可独立修改的数据库字段。

用户说“第二段短一点”时，Script Agent 返回目标 section 的修改结果；业务代码基于当前版本复制出 `version + 1`，原版本不可覆盖。

---

# 22. Content Review Schema

Review 结果必须绑定具体稿件版本：

```python
class ReviewResult(BaseModel):
    review_id: str
    draft_id: str
    draft_version: int
    status: Literal["pass", "revise", "need_research"]
    summary: str
    issues: list[str]
    must_fix: list[str]
    risk_items: list[str]
    revision_request: str | None
    research_request: str | None
    ready_for_generation: bool
    reviewed_at: datetime
```

只要 `draft_version` 发生变化：

```text
旧 Review 保留为历史记录
但 active_review 立即失效
ready_for_generation 不得沿用到新版本
```

---

# 23. Supervisor 是否需要结构化输出

如果 Supervisor 直接面向用户回复：

```text
不要求每次最终输出都是 Schema。
```

但需要保证：

```text
内部 specialist tools 返回结构化数据。
```

Supervisor 最终负责：

```text
把 Agent 结果整理成自然语言回复。
```

例如 Topic Agent 返回结构化 5 个选题。

Supervisor 可以为候选卡片补充自然语言说明，但“1 / 2 / 3”必须来自已经持久化的 `candidate_batch_id + display_index`，不能临时重新排序。

前端优先直接渲染 TopicResult 的结构化卡片；Supervisor 文本只是解释层，不是顺序真相源。

---

# 24. Skills / Principles

LangChain / LangGraph 本身不会替你定义业务 Skill。

本项目继续采用独立 Markdown Principles。

建议：

```text
principles/
├── topic.md
├── research.md
├── script.md
└── content_review.md
```

Agent instructions 动态加载对应 Principles。

---

# 25. Dynamic Prompt / Middleware

V6 不把所有动态上下文拼成一个巨型 system prompt。

每个 Agent 的最终 prompt 由以下组成：

```text
基础角色说明
+
Agent 边界
+
对应 Principles
+
当前任务必要上下文
```

静态部分：

```text
system_prompt
```

动态部分优先通过：

```text
LangChain Middleware / dynamic_prompt
ToolRuntime.context
ToolRuntime.state
```

按需注入。

例如 Supervisor dynamic prompt 只需要知道：

```text
当前 task_id
当前候选 batch
current draft pointer
review / generation 状态摘要
```

不需要把全部历史稿件和全部 Memory 每轮都塞进去。

专业 Agent 则由 wrapper 明确构造所需输入，实现 context engineering。

---

# 26. Topic Principles

由项目负责人填写：

```text
什么是好选题
什么值得做
什么属于高时效
什么不值得做
什么算重复
爆款能否跟
账号人群
内容方向
什么情况下需要调研
```

---

# 27. Research Principles

由项目负责人填写：

```text
什么来源可信
官方来源优先级
怎样判断仍在招聘
不同来源冲突如何处理
哪些信息必须核实
哪些二手内容可以作为线索
什么情况下不能确认
```

---

# 28. Script Principles

由项目负责人填写：

```text
IP 人设
语气
Hook
前三秒
前 15 秒
信息密度
节奏
广告感
内容结构
CTA
常见禁忌
```

---

# 29. Content Review Principles

由项目负责人填写：

```text
通过标准
必须修改问题
事实风险
内容风险
平台风险
人设偏离
过度承诺
表达拖沓
什么情况退回 Script
什么情况退回 Research
```

---

# 30. “今天做啥好呢”的运行方式

用户：

```text
今天做啥好呢
```

Supervisor 根据 Thread State 知道：

```text
账号是谁
之前聊过什么
```

根据 Task State 知道：

```text
当前是否正在做一条内容
```

如果是新任务：

Supervisor 可以：

```text
调用 Topic Agent
```

Topic Agent：

```text
自己决定调用哪些 Tool
```

例如：

```text
search_platform_content
search_recruitment
get_content_history
```

如果信息不够，Topic Agent返回：

```text
research_needed
```

Supervisor可以再调用：

```text
Research Agent
```

然后再次调用：

```text
Topic Agent
```

整个路径不是代码预先固定。

---

# 31. “这个方向可以，但换几个还在招聘的”

Thread State / 当前 candidate batch 提供：

```text
上一轮候选
```

Task State提供：

```text
selected_topic
```

Supervisor理解：

```text
保留 topic direction
修改 entity candidates
要求当前仍在招聘
```

Supervisor可以直接：

```text
调用 Research Agent
```

Research Agent搜索并核实。

必要时：

```text
Supervisor再调用 Topic Agent
```

重组内容方向。

---

# 32. “写吧”

Supervisor必须先识别：

```text
当前是否有 confirmed_topic
```

如果有：

```text
调用 Script Agent
```

如果只是有候选但没有明确选中：

Supervisor应根据对话判断：

```text
“写吧”是否明确指向某个候选。
```

例如上一轮用户说：

```text
第三个不错
```

这一轮：

```text
写吧
```

可以自然推断：

```text
第三个为 confirmed_topic
```

不需要机械重复确认。

---

# 33. “略微改一下”

Script Agent需要：

```text
读取 current_draft
```

根据用户反馈：

```text
局部修改
```

保存：

```text
v2
v3
v4
```

每次修改后：

```text
save_draft_version
```

保存新版本成功后才能把它设为 `current_draft_version` 并返回给用户。

如果保存失败：

```text
不更新 current_draft
不显示“已修改完成”
返回可重试错误
```

任何新版本产生后，必须同时：

```text
失效旧 active_review
失效旧 generation_authorization
不自动取消已经提交的历史数字人任务
```

---

# 34. “生成吧”

这是本项目最重要的 HITL / side-effect 边界之一。

生成授权必须绑定：

```text
task_id
draft_id
draft_version
generation_params_hash
authorized_at
authorization_source_message_id
```

明确授权示例：

```text
生成吧
用这版生成数字人
这版可以，直接生成
```

以下表达**不单独视为数字人生成授权**：

```text
就这版
这版可以
差不多了
可以了
```

除非上一轮 Assistant 的问题明确是：

```text
“是否用 v3 生成数字人？”
```

此时用户回答“就这版 / 可以”才可以绑定为对该版本的生成授权。

一旦稿件继续修改为新版本：

```text
旧 authorization 仍保留审计记录
但不能授权新版本
新版本必须获得新的明确授权
```

授权完成后不需要机械重复询问。

---

# 35. LangGraph HITL 与应用层授权

V6 仍然把“数字人生成授权”首先定义为**业务授权对象**，而不是框架级按钮确认。

流程：

```text
Supervisor 理解用户明确生成意图
↓
业务代码创建 GenerationAuthorization
↓
Digital Human Tool 校验：
- authorization 是否存在
- 是否绑定当前 draft_id + version
- params hash 是否一致
- 是否已经因新版本失效
↓
满足 → 执行
不满足 → 不执行，返回 Supervisor
```

LangChain / LangGraph 的 HITL 能力用于下面场景：

```text
Tool 调用需要额外审批
状态存在歧义，需要暂停等待用户
未来增加“Approve / Edit / Reject”式 UI
长任务需要 interrupt 后跨请求 resume
```

如果启用框架级 HITL：

```text
HumanInTheLoopMiddleware
+
LangGraph checkpointer
+
thread_id
```

共同完成 interrupt / resume。

但第一版不要把：

```text
“用户已经说生成吧”
```

再机械变成一次重复审批。

应用层 Authorization 是业务真相；framework interrupt 是运行控制机制，两者不能混为一谈。

---

# 36. Digital Human Tool

建议 Tool 输入包含明确对象与幂等信息：

```python
@tool
async def generate_digital_human(
    runtime: ToolRuntime[ContentAppContext, ContentAgentState],
    draft_id: str,
    draft_version: int,
    generation_authorization_id: str,
    generation_params: GenerationParams,
) -> DigitalHumanTask:
    ...
```

Tool 内部顺序固定：

```text
1. 读取 authorization
2. 校验 draft_id + version + params hash
3. 计算 idempotency_key
4. 查询是否已有相同 key 的 generation record
5. 已有 → 返回已有任务，不重复提交
6. 没有 → 创建 pending record
7. 调用现有数字人 API
8. 保存 provider_task_id / 状态
9. 返回 generation_id
```

建议：

```text
idempotency_key
= hash(task_id + draft_id + draft_version + generation_params_hash)
```

这样网络重试不会生成两个相同视频任务。

---

# 37. 数字人状态查询

如果数字人 API 是异步：

```text
提交生成
↓
返回 provider_task_id
↓
业务层保存 generation_id ↔ provider_task_id
↓
前端轮询 / SSE 更新
↓
完成
```

不要让 LLM 自己长时间等待。

如果“提交请求超时，但无法确认供应商是否已经受理”：

```text
禁止直接再次提交
先按 idempotency_key / provider 查询接口尝试确认
仍无法确认 → 状态记为 unknown_submission
返回用户“提交状态待确认”
```

只有确认未受理后才允许重试创建新供应商任务。

---

# 38. Agent Invocation

V6 不再使用 OpenAI Agents SDK 的 `Runner.run()` / `Runner.run_streamed()`；统一使用 LangChain Agent / LangGraph runtime 的 `.invoke()`、`.ainvoke()`、`.stream()`、`.astream()`。

每次用户发消息：

```text
Go 收到 user_message + request_id
↓
代理到 Python Agent Service
↓
按 session_id / thread_id 进入串行队列
↓
加载 Business State pointer
↓
构建 ContentAppContext
↓
Supervisor.ainvoke(...)
↓
LangGraph Checkpointer 自动保存 thread state
↓
业务 Tool 在 SQLite 事务中写入 canonical object
↓
返回 final response
↓
释放 session lock
```

示意：

```python
config = {
    "configurable": {"thread_id": session_id},
    "recursion_limit": 12,
    "tags": ["content-agent", f"session:{session_id}"],
}

result = await supervisor.ainvoke(
    {"messages": [{"role": "user", "content": user_message}]},
    config=config,
    context=app_context,
)
```

第一版同一个 `session_id` 必须串行执行，防止：

```text
两个并发请求都读取 draft v2
→ 同时创建两个 v3
```

最简单实现：

```text
per-session asyncio.Lock / request queue
+
state_revision optimistic check
```

即使有 LangGraph Checkpointer，也不能用 checkpoint 替代业务数据库的并发校验。

## 38.1 状态更新硬规则

这些规则属于业务代码，不属于 Supervisor Prompt。

| 事件 | 必须执行的状态变化 |
|---|---|
| 创建新候选批次 | 保存新 `candidate_batch_id` 与 display mapping |
| 用户确认新 topic | `confirmed_topic` 更新；旧 draft/review/auth 不再属于当前分支 |
| 切换到不同 topic | 保留历史稿件，但 `current_draft=null`，`active_review=null`，generation authorization 失效 |
| Research 更新 | 保存新 `research_result_id`；是否重写稿件由 Agent 决定，不静默覆盖 |
| 生成新 Draft | 创建新 `draft_id v1` 或同 draft 新 version；事务成功后再设为 current |
| 修改 Draft | version + 1；旧 review 与旧 generation authorization 失效 |
| Review 完成 | 绑定 `draft_id + version`；只更新该版本 review |
| 用户授权生成 | 创建绑定当前 `draft_id + version + params_hash` 的 authorization |
| 数字人提交 | 用 idempotency key 创建/复用 generation record |
| 数字人完成 | 只更新对应 generation_id，不把历史任务结果写到当前新版本上 |

### 38.1.1 切换选题不是删除历史

如果用户从 Topic A 切到 Topic B：

```text
Topic A 的 Draft / Review / Generation history 仍保留
但当前 task pointer 切到 Topic B
```

以后用户说“刚才 A 那版”时，可以显式恢复该分支；不能把 A 的 v3 直接当成 B 的当前稿件。

### 38.1.2 写入成功后再展示

对 Topic confirmation、Draft version、Review、Authorization、Generation submit：

```text
数据库 commit 成功
↓
更新 Agent State pointer
↓
向用户/前端发送“完成”事件
```

禁止先告诉用户“已保存 / 已生成”，再发现数据库写入失败。

---

# 39. Streaming

V6 使用 LangGraph / LangChain agent 的 `.astream()` / `.stream()`。

前端至少需要两类流：

```text
messages
= LLM token / 最终文本

custom
= 业务进度事件
```

必要时增加：

```text
updates
= Graph / Agent state update
```

示意：

```python
async for chunk in supervisor.astream(
    {"messages": [{"role": "user", "content": user_message}]},
    config=config,
    context=app_context,
    stream_mode=["messages", "custom"],
    version="v2",
):
    ...
```

Tool 内需要向前端发送业务进度时，使用 custom stream writer：

```text
research_started
source_verified
draft_saved
generation_submitted
```

不要把“正在调研...”之类状态依赖于 LLM 自己输出。

---

# 40. 前端 Agent 工作动画

前端“小牛马工作动画”不是 Agent 框架本身。

它由：

```text
LangGraph stream events
Tool events
Agent tool calls
```

驱动。

前端可以展示：

```text
选题员正在工作
调研员正在搜索
写稿员正在写稿
审核员正在检查
数字人正在生成
```

---

# 41. 前端状态事件

后端建议统一发送：

```json
{
  "type": "agent_status",
  "request_id": "req_xxx",
  "session_id": "session_xxx",
  "task_id": "task_xxx",
  "agent": "research",
  "status": "working",
  "message": "正在核实招聘状态"
}
```

其他：

```text
agent_started
agent_finished
tool_started
tool_finished
draft_updated
waiting_user
generation_started
generation_completed
```

---

# 42. Middleware 与业务事件

V6 不把前端状态动画主要建立在 SDK Hooks 上。

LangChain Middleware 负责 Agent 运行层扩展，例如：

```text
动态 prompt
模型调用前后处理
Tool error 处理
重试策略
上下文裁剪
安全边界
```

前端业务事件则由 Tool / Service 主动发送：

```text
agent_started
agent_finished
tool_started
tool_finished
draft_updated
generation_started
generation_completed
```

通过 LangGraph custom streaming / Event Bus 推给前端。

这样：

```text
Middleware
= Agent runtime concern

Business Event
= 产品 UI concern
```

两者不混用。

---

# 43. LangSmith Tracing

V6 第一版优先使用 LangSmith 做 Agent Trace。

重点查看：

```text
Supervisor model call
Subagent tool call
Specialist model call
普通 Tool call
latency
token / usage
error
stateful thread run
```

作品集可以展示一次真实 Trace，让面试官看到：

```text
Supervisor 为什么调用 Research
Research 调了哪些 Tool
返回结果后 Supervisor 又做了什么
```

本地业务数据仍需单独记录，LangSmith 不是业务数据库。

---

# 44. 自定义业务 Trace

除了 LangSmith，建议本地保存：

```text
request_id
session_id / thread_id
task_id
user_message
specialist agent calls
tool calls
topic selection
draft version changes
review version
generation authorization
generation task
latency
error_code
```

原因：

```text
LangSmith = Agent 可观测性
本地 Business Trace = 产品状态审计
```

作品集前端如果需要自定义时间线，优先读取本地业务 Trace，而不是强耦合 LangSmith UI。

---

# 45. Guardrails / Middleware

V6 的技术安全边界优先通过：

```text
Tool 参数 Schema
Pydantic validation
Middleware
数据库约束
业务校验
```

处理。

例如：

```text
Content Review Agent
= 业务内容审核

Middleware / Validation
= 技术运行边界
```

不要再造一个“Guardrail Agent”重复 Content Review。

推荐第一版加入：

```text
Tool 参数校验
模型 / Tool timeout
Tool exception 统一转换
危险 side-effect Tool 前置业务校验
长对话消息裁剪 / summarization（需要时）
```

---

# 46. Agent 边界

## Supervisor

负责：

```text
用户交互
理解上下文
调度
整合结果
决定继续或返回用户
```

## Topic Agent

负责：

```text
选题
内容方向
候选比较
```

## Research Agent

负责：

```text
搜索
核实
资料
来源
```

## Script Agent

负责：

```text
写稿
改稿
版本
```

## Content Review Agent

负责：

```text
审核
风险
返工建议
```

---

# 47. 第一版不手写完整 StateGraph

V6 使用 LangGraph，但不意味着要把所有业务重新画成 Node / Edge。

第一版主入口仍然是：

```text
LangChain create_agent(Supervisor)
```

因为 `create_agent` 本身已经运行在 LangGraph runtime 之上，可以直接获得：

```text
checkpointer
thread state
interrupt
streaming
持久化运行
```

所以第一版不额外写：

```text
START
→ Topic Node
→ Research Node
→ Script Node
→ Review Node
→ END
```

这种固定图。

只有当某块业务确实需要：

```text
严格顺序
确定性分支
循环上限
暂停 / 恢复
并行 fan-out / fan-in
复杂状态迁移
```

才将该块封装为 Custom StateGraph，并把它作为 Tool 或 Node 接入 Supervisor。

因此 V6 是：

```text
Agent-first
Graph-when-needed
```

而不是：

```text
Everything-is-a-Graph
```

---

# 48. 自主性来自哪里

自主性不是：

```text
框架帮你写了一个自动流程
```

而是：

```text
Supervisor看到多个可用专业 Agent
↓
根据当前任务自己决定调用谁

专业 Agent看到多个 Tool
↓
根据当前问题自己决定调用哪个

Agent拿到结果
↓
自己判断是否还需要继续调查
```

程序只提供：

```text
能力边界
状态
调用安全
```

---

# 49. 模型选择

LangChain 的优势之一是统一模型接口，不把系统绑定在单一 Provider。

本项目可以按角色选择模型：

```text
Supervisor
→ 工具调用和上下文理解稳定的模型

Topic / Research
→ 工具调用稳定、成本适中的模型

Script
→ 可以继续使用 DeepSeek

Review
→ 结构化输出稳定的模型
```

第一版不要求所有 Agent 一定使用不同模型。

推荐原则：

```text
先减少变量
再按 Agent 特性拆模型
```

---

# 50. DeepSeek 接入

LangChain 当前提供独立：

```text
langchain-deepseek
ChatDeepSeek
```

第一版如果 Script Agent 直接使用 DeepSeek，建议使用：

```python
from langchain_deepseek import ChatDeepSeek

script_model = ChatDeepSeek(
    model="deepseek-chat",
    temperature=0.7,
    max_retries=2,
)
```

当前官方集成标注：

```text
deepseek-chat
支持 tool calling
支持 structured output
支持 streaming

DeepSeek reasoner
不适合作为依赖 tool calling / structured output 的 Agent 主模型
```

因此：

```text
Script Agent 如需 Tool / response_format
→ 优先 deepseek-chat
```

如果现有 Go 写稿服务已经沉淀了稳定 Prompt / Skill，也可以暂时保留：

```text
Script Agent
↓
调用 existing Go writing tool
```

第一版二选一，不做双重生成链路。

---

# 51. 推荐的第一版模型策略

为了减少开发变量：

```text
Supervisor / Topic / Research / Review
→ 先统一一套工具调用稳定模型

Script
→ 保留已经验证过的 DeepSeek 路线
```

理由：

```text
Script 是当前最有真实业务验证的模型链路
不需要为了框架统一而重写
```

等 Agent 架构跑通后，再根据：

```text
质量
延迟
成本
Tool reliability
structured output reliability
```

逐 Agent 调整。

---

# 52. 前端

前端值得保留。

作品集需要看到：

```text
这是一个产品
```

而不是：

```text
只有 Python terminal。
```

---

# 53. 前端布局

建议双栏。

左侧：

```text
聊天区
```

右侧：

```text
当前任务
当前选题
当前稿件
审核状态
生成状态
Agent 工作状态
```

---

# 54. 聊天区

支持：

```text
文字消息
选题卡片
来源链接
稿件
视频结果
```

---

# 55. Agent 工作区

可以做成：

```text
Topic
Research
Script
Review
```

四个小角色。

状态：

```text
idle
working
done
waiting
error
```

---

# 56. “小牛马工作动画”

可以纯前端实现。

例如：

```text
Research Agent start
↓
小人走到电脑前
↓
显示“正在搜索”

Tool call
↓
出现搜索动画

Research Agent end
↓
显示“已找到 8 条信息”
```

不影响后端 Agent 架构。

---

# 57. 现有网页如何迁移

不用推倒。

原有：

```text
选题功能
稿件功能
数字人功能
```

全部保留底层函数。

前端逐步增加：

```text
/chat
/session
/task-state
/events
```

---

# 58. API 建议

第一版区分 public API 与 internal API。

## 58.1 Browser → Go public API

```text
POST /api/agent/chat
POST /api/agent/chat/stream
GET  /api/agent/sessions/{id}
GET  /api/agent/tasks/{session_id}
GET  /api/agent/events/{session_id}
GET  /api/agent/generation/{generation_id}
```

`POST /api/agent/chat` 至少携带：

```json
{
  "session_id": "session_xxx",
  "request_id": "req_xxx",
  "message": "第三个写吧"
}
```

返回中保留：

```text
request_id
session_id
task_id
state_revision
final_output
structured_cards（如有）
```

## 58.2 Python Agent Service internal API

```text
POST /internal/agent/chat
POST /internal/agent/chat/stream
GET  /internal/agent/tasks/{session_id}
GET  /internal/agent/events/{session_id}
```

## 58.3 Go capability internal API

真实路径在 Step 0 代码审计后确定。逻辑上至少需要：

```text
search_platform_content
search_recruitment
fetch_source
generate_digital_human
get_generation_status
```

统一错误响应建议：

```json
{
  "error_code": "SEARCH_TIMEOUT",
  "message": "search service timeout",
  "retryable": true,
  "request_id": "req_xxx"
}
```

不要让 Agent 根据任意异常字符串猜“能不能重试”。

---

# 59. 目录结构建议

现有仓库仍然保留 Go 主体，新建 Python Agent Service。

```text
repo/
├── main.go
├── handlers/
├── llm/
├── topicsearch/
├── chanjing/
│
├── internal/
│   └── agentproxy/                 # Go → Python 代理
│
└── agent_service/
    ├── app.py
    │
    ├── agents/
    │   ├── supervisor.py
    │   ├── topic.py
    │   ├── research.py
    │   ├── script.py
    │   └── review.py
    │
    ├── agent_tools/
    │   ├── topic_expert.py         # subagent wrapper
    │   ├── research_expert.py
    │   ├── script_expert.py
    │   └── review_expert.py
    │
    ├── tools/
    │   ├── go_search.py
    │   ├── content_history.py
    │   ├── task_state.py
    │   ├── drafts.py
    │   └── digital_human.py
    │
    ├── schemas/
    │   ├── topic.py
    │   ├── research.py
    │   ├── draft.py
    │   ├── review.py
    │   ├── state.py
    │   └── generation.py
    │
    ├── runtime/
    │   ├── context.py
    │   ├── checkpointer.py
    │   ├── model_factory.py
    │   ├── middleware.py
    │   └── streaming.py
    │
    ├── services/
    │   ├── task_store.py
    │   ├── memory_store.py
    │   ├── authorization_service.py
    │   ├── generation_service.py
    │   └── event_bus.py
    │
    ├── principles/
    │   ├── topic.md
    │   ├── research.md
    │   ├── script.md
    │   └── content_review.md
    │
    ├── workflows/                  # 第一版可为空
    │   └── README.md               # 只有需要确定性 Graph 时才新增
    │
    └── db/
        ├── schema.sql
        ├── app.sqlite              # Business State / Content Memory
        └── checkpoints.sqlite      # LangGraph Checkpointer
```

注意：

```text
agents/
= 专业 Agent 本身

agent_tools/
= 把专业 Agent 暴露给 Supervisor 的 wrapper

workflows/
= 后续真正需要 Custom StateGraph 的局部流程
```

不要一开始就在 `workflows/` 里人为搭出 Topic → Research → Script → Review 固定链路。

---

# 60. 当前已有能力复用

本节只定义“复用策略”，不在未审代码的情况下认定具体函数名。

## 60.1 搜索

Step 0 确认：

```text
现有搜索入口在哪里
输入输出结构是什么
是否有同步/异步限制
是否已有超时/重试
```

优先：

```text
Go 内部函数
→ 包一层 internal HTTP adapter
→ Python Function Tool 调用
```

搜索 Tool 只返回事实数据，不返回“推荐做哪个选题”之类业务结论。

## 60.2 写稿

现有 DeepSeek 能力有两种复用方式：

```text
A. Script Agent 直接使用 DeepSeek 作为 model
B. Script Agent 调用现有 Go 写稿服务 Tool
```

第一版只选一种主路径，避免“Agent 自己生成 + 又调旧写稿服务”双重逻辑。

选择标准：

```text
如果现有写稿 prompt / skill 已经过真实业务验证且封装较深
→ 优先 B，先复用

如果需要完整 Agent structured output / 局部版本修改
且旧服务不便扩展
→ 评估 A
```

## 60.3 数字人

现有数字人 API 继续复用，但 Python 不直接相信“调用成功”就等于“业务已生成”。

必须经过：

```text
Authorization
Idempotency record
Provider submit
Provider task mapping
Status query
```

---

# 61. 开发执行计划｜Codex / GPT-6 分任务版

本节是 **V6 最重要的执行规范**。

前面的章节回答：

```text
系统最终应该是什么
架构边界是什么
状态如何设计
Agent / Tool 如何分工
```

本节回答：

```text
Codex 这一轮到底只做什么
做到哪里算完成
验证什么
什么时候必须停止
下一轮从哪里继续
```

目标不是让 Codex “一次性完成整个项目”，而是把项目拆成**可独立验收、可恢复上下文、可在任意一轮安全停止**的工程任务。

---

## 61.0 全局执行协议｜每一轮都必须遵守

### 61.0.0 唯一执行入口原则

从 V6.1 开始，本文件是本项目**唯一的人类执行说明入口**。

用户后续不需要再同时提供：

```text
V3 / V4 / V5 开发文档
单独的 Prompt 文档
单独的任务拆解文档
单独的开发路线图
```

Codex / GPT-6 每一轮只需要获得：

```text
1. 本文件
2. 当前真实代码仓库
3. 当前 Task 的 ID（Phase 模式下为当前 Phase 编号，见 61.0.1a）
```

但“只用这一个文档”**不等于忽略前序 Task 产物**。前序 Task 在仓库中生成的审计、契约、Schema、测试或代码文件属于项目工程事实，后续 Task 必须按各自“前置条件 / 前置产物”读取。

因此正确理解是：

```text
唯一说明源 = 本 V6.1 文档
工程事实源 = 当前代码仓库 + 前序 Task 已提交产物
```

如果本文件与真实仓库事实冲突：

```text
真实仓库事实优先
→ 当前 Task 标记 BLOCKED
→ 报告冲突
→ STOP
```

禁止为了让代码“符合文档”而擅自大规模重构现有稳定实现。

### 61.0.1 一轮只执行一个 Task

每次给 Codex / GPT-6 的指令必须明确指定唯一 Task ID，例如：

```text
执行 V6 文档的 Task 2.4。
只做 Task 2.4，不做 2.5，也不要提前实现后续能力。
完成本 Task 的验收后立即停止。
```

禁止：

```text
“继续开发这个项目”
“把 Phase 2 做完”
“按文档往下做”
```

因为这种指令没有停止边界，模型很容易连续工作直到上下文、token、运行时或 5h 使用限制被耗尽。

### 61.0.1a Phase 模式｜用户显式授权后生效

默认仍然是 61.0.1 的单 Task 模式。只有当用户在指令里明确写出“允许按阶段执行”或“Phase 模式”时，本轮才切换为 Phase 模式：

```text
一轮的目标 = 一个 Phase 的全部 Task
```

Phase 模式下的规则：

```text
1. 按 Task 编号升序逐个执行，每个 Task 仍然完整走一遍 61.0.2 的全部步骤：
   读卡片 → 读前置产物 → 检查已有实现 → 只改允许范围 → 实现 → 执行该 Task 的验收 → 该 Task 单独提交
2. 每个 Task 必须通过它自己卡片规定的全部验收，不允许因为“整段一起测”而减少任何一个 Task 的验证
3. 任何 Task 验收不通过，就地停止整个 Phase，输出到该 Task 为止的汇总报告并 STOP；
   不允许跳过该 Task 继续后面的 Task
4. 每个 Task 保持独立提交边界（61.0.6），Phase 模式不合并提交
5. Task 卡片下方的固定 Prompt 中，“本轮唯一目标：完成并验收 Task X.X”和
   “绝对不要开始下一个 Task”两句，在 Phase 模式下由本节取代；该 Prompt 其余全部约束
   （允许改动范围、禁止顺手重构、不得提前实现下一 Task 的能力）继续有效
6. 报告粒度改为 Phase 末：输出一份 Phase Report，内含该 Phase 每个 Task 的 61.0.3 格式报告；
   Phase 进行中不中途停下来等用户确认
7. Phase 模式不改变 61.0.4（BLOCKED）和 61.0.5（禁止顺手重构）的任何判定
8. 预算护栏：如果上下文或额度接近上限，输出到当前 Task 为止的汇总报告并 STOP，
   不允许为了让 Phase “跑完”而压缩任何一个 Task 的验收
```

Phase Report 固定格式：

```text
PHASE <N> DONE / BLOCKED AT <Task ID>

完成：
- Task X.X：（该 Task 的完成摘要）
- Task X.X：（该 Task 的完成摘要）

修改文件：
- （按 Task 分组）

验证：
- 命令：...
- 结果：PASS / FAIL（按 Task 分别列出）

发现但未处理：
- ...

下一 Phase：
- Phase X.X（仅提示，不执行）

STOP HERE.
```

### 61.0.2 每个 Task 的固定执行顺序

Codex 每轮必须按以下顺序执行：

```text
1. 读取当前 Task 卡片
2. 读取“前置产物”中明确列出的文档 / 文件
3. 检查当前仓库是否已经部分完成该 Task
4. 只修改“允许改动范围”内的文件
5. 完成实现
6. 执行本 Task 指定的验证
7. 输出 Task Report
8. 立即 STOP
```

不能在第 7 步之后继续下一 Task。

### 61.0.3 Task Report 固定格式

每个 Task 结束时，Codex 最后只需要汇报：

```text
TASK <ID> DONE / BLOCKED

完成：
- ...

修改文件：
- ...

验证：
- 命令：...
- 结果：PASS / FAIL

发现但未处理：
- ...

下一任务：
- Task X.X（仅提示，不执行）

STOP HERE.
```

如果失败：

```text
TASK <ID> BLOCKED
```

必须说明阻塞原因、已经验证的事实和需要决策的点，然后停止。

### 61.0.4 强制停止规则

出现以下任意情况，**不得扩大范围自行解决**，必须停止并报告：

```text
A. 发现 V6 与真实代码严重不一致，需要重新做架构决策
B. 当前 Task 必须修改一个未被“允许改动范围”覆盖的核心模块
C. 需要删除 / 大规模重构现有稳定能力
D. 发现真实 API 行为与 Phase 0 契约不一致
E. 需要新增数据库主表或改变核心状态语义，但当前 Task 没有授权
F. 需要引入新的 Agent / 新框架 / 新基础设施
G. 测试失败原因来自另一个尚未实施的 Task
H. 同一问题连续修复后仍无法通过当前 Task 验收
I. 当前 Task 的实现开始自然延伸到下一 Task
```

这时正确行为是：

```text
记录 blocker
保留当前已完成工作
说明建议
STOP
```

而不是“顺便一起做掉”。

### 61.0.5 禁止顺手重构

除非当前 Task 明确要求，否则不允许：

```text
顺手统一目录
顺手改命名
顺手重构旧 Go 代码
顺手换 ORM
顺手引入 Redis / PostgreSQL
顺手做前端
顺手做完整测试体系
顺手接所有 Agent
顺手补下一阶段功能
```

每个 Task 只为自己的验收标准负责。

### 61.0.6 Git 建议

如果仓库已经使用 Git，每个 Task 最好对应一个独立提交边界：

```text
Task 1.3 完成
→ 测试 PASS
→ commit
→ 再开始 Task 1.4
```

文档不强制 Codex 自动 commit；如果用户没有要求，不自动执行 `git commit`。

### 61.0.7 每轮启动方式

因为每个 Task 下方已经内置了“本 Task 可复制执行 Prompt”，用户不需要自己再组合长 Prompt。

每次只需要：

```text
读取 AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。
执行 Task X.X。
```

随后 Codex 必须使用该 Task 卡片下方内置 Prompt 的完整约束执行。

如果当前环境不能自动读取本文件，则把对应 Task 下方的“本 Task 可复制执行 Prompt”原样粘贴给它。

Phase 模式（61.0.1a）下的启动方式：

```text
读取 AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。
允许按阶段执行。
执行 Phase 6 的全部 Task。
```

“允许按阶段执行”这句话是切换开关，必须由用户明说；没有这句话时仍然是单 Task 模式。Phase 模式下不需要逐条粘贴 Task 卡片 Prompt，因为 61.0.1a 已经规定每个 Task 仍按自己卡片的允许改动范围和验收执行。

### 61.0.8 BLOCKED 后重试 Prompt

某个 Task 上一轮为 `BLOCKED` 时，不允许直接说“继续”。使用：

```text
读取 V6.1 文档。
重新执行 Task X.X。
先读取上一轮 Task Report 和当前仓库状态。
只解决导致 Task X.X BLOCKED 的问题；仍然禁止进入下一 Task。
完成本 Task 原有验收后输出新的 Task Report 并 STOP。
```

### 61.0.9 已完成 Task 的复核 Prompt

如果怀疑某个 Task 虽然报告 DONE，但实现有问题，使用：

```text
读取 V6.1 文档。
只复核 Task X.X，不开发任何新功能。
根据该 Task 的目标、允许改动范围和验收标准检查当前仓库。
如发现问题，只修复属于 Task X.X 范围的问题并重新验收。
输出 Task Report 后 STOP。
不要进入下一 Task。
```

---

# Phase 0｜真实代码审计与接口冻结

**Phase 目标：** 不写 Agent，不做重构。先把现有 Go 项目到底已经有什么能力查清楚。

**Phase 完成门槛：** Task 0.1～0.6 全部 DONE 后，才能进入 Phase 1。

---

## Task 0.1｜仓库地图与启动链路审计

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 0.1｜仓库地图与启动链路审计。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 0.1 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 0.1，避免重复实现。
4. 只修改 Task 0.1 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 0.1 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 0.1。
```

### 目标

只回答：

```text
这个 Go 项目从哪里启动？
前端资源在哪里？
主要模块是什么？
配置从哪里读取？
```

### 前置条件

```text
仓库可正常读取
不要求项目已经能启动
```

### 允许改动范围

只允许新增：

```text
docs/existing-capabilities-audit.md
```

如果 `docs/` 不存在可以创建。

**禁止修改任何业务代码。**

### 具体步骤

1. 找到 `main.go` / 实际启动入口。
2. 找到 router / handler 注册方式。
3. 找到静态前端资源目录、模板目录或前端构建产物目录。
4. 列出一级业务模块及作用，不深入分析具体实现。
5. 找到环境变量、配置文件、密钥读取方式。
6. 找到当前端口与启动命令。
7. 将结果写入 `existing-capabilities-audit.md` 的“仓库地图”章节。

### 验收

文档必须至少包含：

```text
启动入口
启动命令
HTTP 路由注册位置
前端资源位置
主要业务目录表
配置来源
尚未确认的问题
```

### 本 Task 不做

```text
不分析搜索实现细节
不分析 DeepSeek Prompt
不分析数字人 API
不新增 Python
不改 Go
```

### STOP 条件

`existing-capabilities-audit.md` 已写完仓库地图，并能让下一轮快速定位各模块。

**到此立即停止。不要开始 Task 0.2。**

---

## Task 0.2｜搜索能力审计

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 0.2｜搜索能力审计。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 0.2 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 0.2，避免重复实现。
4. 只修改 Task 0.2 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 0.2 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 0.2。
```

### 目标

只查清现有：

```text
选题搜索
招聘搜索
网页抓取 / 来源获取
```

真实调用链。

### 前置产物

读取：

```text
docs/existing-capabilities-audit.md
```

### 允许改动范围

只允许更新：

```text
docs/existing-capabilities-audit.md
```

禁止改搜索代码。

### 具体步骤

1. 找到搜索相关 package / handler / service。
2. 追踪一次完整调用：HTTP 输入 → 业务函数 → 外部请求 → 返回值。
3. 记录真实函数名和文件路径。
4. 记录输入字段、输出字段。
5. 记录是否联网、调用哪个搜索源。
6. 记录现有 timeout / retry / error handling。
7. 判断哪些能力可直接包装成 internal HTTP API。
8. 标记已经停用或不稳定的搜索链路，不做修复。

### 验收

审计文档新增“Search Capability”章节，必须能回答：

```text
Python 将来应该调用哪个 Go 能力？
需要新增 adapter 还是已有 HTTP API？
返回的是事实数据还是已经混入业务判断？
```

### 本 Task 不做

```text
不修搜索质量
不换搜索引擎
不接 LangChain Tool
不重新设计 Topic Agent
```

### STOP 条件

搜索调用链、真实 I/O、可复用点全部写清。

**立即停止。不要开始 Task 0.3。**

---

## Task 0.3｜DeepSeek / 写稿能力审计

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 0.3｜DeepSeek / 写稿能力审计。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 0.3 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 0.3，避免重复实现。
4. 只修改 Task 0.3 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 0.3 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 0.3。
```

### 目标

确认现有口播生成到底如何工作，以及第一版应该：

```text
A. Script Agent 直接调用 DeepSeek
还是
B. Script Agent 复用现有 Go 写稿服务
```

本 Task 只收集事实，不做最终重构。

### 前置产物

```text
docs/existing-capabilities-audit.md
```

### 允许改动范围

只更新该审计文档。

### 具体步骤

1. 找到 DeepSeek client 初始化。
2. 找到写稿入口。
3. 找到 Prompt / Skill / 模板来源。
4. 找到模型参数和输出解析。
5. 找到是否支持局部改稿。
6. 找到是否已有历史上下文。
7. 记录当前业务中真正使用的稳定链路。
8. 给出 A/B 复用建议，但只作为审计结论。

### 验收

文档明确列出：

```text
真实入口
真实 Prompt/Skill 路径
请求/返回结构
已验证的业务能力
当前限制
推荐复用模式 A 或 B + 原因
```

### 本 Task 不做

```text
不改 Prompt
不迁移 DeepSeek
不写 Script Agent
不写 LangChain
```

### STOP 条件

已经可以在不重新搜索代码的情况下回答“旧写稿能力如何复用”。

**立即停止。**

---

## Task 0.4｜数字人能力审计

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 0.4｜数字人能力审计。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 0.4 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 0.4，避免重复实现。
4. 只修改 Task 0.4 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 0.4 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 0.4。
```

### 目标

完整确认：

```text
数字人提交
任务 ID
状态查询
结果 URL
错误处理
```

### 允许改动范围

只更新：

```text
docs/existing-capabilities-audit.md
```

### 具体步骤

1. 找到数字人 provider / adapter。
2. 找到 submit 请求参数。
3. 找到 provider task id。
4. 找到状态查询接口。
5. 找到完成后的视频 URL 字段。
6. 查是否已有重复提交保护。
7. 查超时时能否判断 provider 已受理。
8. 记录现有错误码。

### 验收

文档能明确画出：

```text
submit → provider_task_id → poll/status → video_url
```

并列出真实字段。

### 本 Task 不做

```text
不实现 authorization
不实现 idempotency
不改数字人 API
```

### STOP 条件

数字人真实调用契约已被记录。

**立即停止。**

---

## Task 0.5｜现有数据、SQLite 与配置审计

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 0.5｜现有数据、SQLite 与配置审计。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 0.5 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 0.5，避免重复实现。
4. 只修改 Task 0.5 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 0.5 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 0.5。
```

### 目标

确认现有项目有没有：

```text
SQLite / DB
已有表
本地文件状态
缓存
用户配置
历史内容数据
```

### 允许改动范围

只更新审计文档。

### 具体步骤

1. 搜索数据库初始化代码。
2. 列出现有表、字段、用途。
3. 确认现有 DB 文件位置。
4. 找到迁移机制；没有则记录“无”。
5. 查是否已有内容历史 / 稿件 / 搜索结果持久化。
6. 查 config / env / secrets。
7. 标记不能与 Agent Business DB 混用的数据。

### 验收

文档有“Data & Config”章节，并明确：

```text
哪些表可复用
哪些数据只能读取
Agent DB 应新建还是复用现有 DB
```

### STOP 条件

数据边界已足以设计 Phase 1 Business DB。

**立即停止。**

---

## Task 0.6｜冻结 Internal API Contract

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 0.6｜冻结 Internal API Contract。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 0.6 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 0.6，避免重复实现。
4. 只修改 Task 0.6 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 0.6 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 0.6。
```

### 目标

基于 0.1～0.5 的真实代码审计，形成 Python ↔ Go 第一版契约。

### 前置产物

```text
docs/existing-capabilities-audit.md
```

### 允许改动范围

允许新增：

```text
docs/internal-api-contract.md
```

允许更新审计文档中的“最终决策”。

禁止写生产 API 代码。

### 具体步骤

1. 为每个需要暴露给 Python 的 Go 能力定义 endpoint。
2. 定义 request schema。
3. 定义 success response。
4. 定义 error response：`error_code / retryable / request_id`。
5. 标记哪些 endpoint 已存在，哪些下一阶段需要新增。
6. 定义 Go → Python agent API 最小契约。
7. 冻结第一版字段名。

### 验收

`internal-api-contract.md` 至少包含：

```text
Go → Python:
POST /internal/agent/chat
GET health

Python → Go:
search...
write...（如果走旧写稿服务）
generate-digital-human
generation-status
```

所有接口都必须有示例 JSON。

### 本 Task 不做

```text
不实现 endpoint
不安装 LangChain
不建数据库
```

### STOP 条件

Phase 1 可以只根据这两份文档开始搭骨架，无需重新猜现有代码。

**Phase 0 到此结束。停止。不要自动进入 Phase 1。**

---

# Phase 1｜Python Agent Runtime 最小骨架

**Phase 目标：** 只建立“能跑、能持久化 thread、能被 Go 调用”的最小 Agent Service，不接完整业务。

---

## Task 1.1｜创建 agent_service 项目骨架

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 1.1｜创建 agent_service 项目骨架。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 1.1 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 1.1，避免重复实现。
4. 只修改 Task 1.1 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 1.1 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 1.1。
```

### 目标

新增 Python 服务目录和依赖声明，但不实现业务 Agent。

### 前置产物

```text
existing-capabilities-audit.md
internal-api-contract.md
```

### 允许改动范围

只新增 / 修改：

```text
agent_service/
依赖文件（pyproject.toml / requirements.txt，择现有项目风格）
.gitignore（仅必要新增 Python / sqlite 忽略项）
```

禁止修改现有 Go 业务。

### 具体步骤

1. 建立 `agent_service/` 基础目录。
2. 建立 Python package。
3. 添加 FastAPI 或 Phase 0 确定的 HTTP 框架。
4. 添加 LangChain / LangGraph / Pydantic / sqlite checkpointer 依赖。
5. 创建最小 `app.py`。
6. 增加 `/health`。
7. 确保服务可独立启动。

### 验收

必须实际运行：

```text
Python service 启动
GET /health → 200
```

### 本 Task 不做

```text
不建业务 DB
不接模型
不建 Supervisor
不接 Go
```

### STOP 条件

Python 服务能独立启动并通过 health check。

**立即停止。**

---

## Task 1.2｜配置、日志与 request_id

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 1.2｜配置、日志与 request_id。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 1.2 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 1.2，避免重复实现。
4. 只修改 Task 1.2 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 1.2 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 1.2。
```

### 目标

把后续所有请求需要的配置和基础日志先固定。

### 允许改动范围

```text
agent_service/config*
agent_service/runtime/*（仅配置/日志相关）
agent_service/app.py
.env.example
```

### 具体步骤

1. 定义配置对象。
2. 支持 `GO_CAPABILITY_BASE_URL`。
3. 支持模型 provider 基础配置占位。
4. 支持 DB path。
5. 增加 request_id middleware。
6. 统一结构化日志最小字段。
7. 禁止打印 API key。

### 验收

发起一次 `/health` 或测试 endpoint，日志能看到：

```text
request_id
path
status
latency
```

### 本 Task 不做

```text
不接 LangSmith
不做完整 middleware
不做业务日志
```

### STOP 条件

配置加载与 request_id 日志通过测试。

**立即停止。**

---

## Task 1.3｜Business SQLite 初始化与 Schema V1

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 1.3｜Business SQLite 初始化与 Schema V1。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 1.3 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 1.3，避免重复实现。
4. 只修改 Task 1.3 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 1.3 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 1.3。
```

### 目标

建立业务数据库骨架，只创建 Phase 2 必需表。

### 允许改动范围

```text
agent_service/db/
agent_service/services/task_store.py（或同职责文件）
agent_service/schemas/state.py
相关单元测试
```

### 第一版只建

```text
content_tasks
candidate_batches
candidate_items
drafts
draft_versions
generation_authorizations
generation_records
```

Research / Review / Long-term Memory 表先不建。

### 具体步骤

1. 根据 V6 schema 定义 SQL。
2. 加主键、外键、唯一约束。
3. `draft_id + version` 唯一。
4. generation `idempotency_key` 唯一。
5. candidate batch 保存 display index。
6. 建 DB init / migrate 最小机制。
7. 写 repository smoke tests。

### 验收

```text
空数据库可初始化
重复初始化不报错
基础 CRUD 测试通过
唯一约束测试通过
```

### 本 Task 不做

```text
不写 Agent
不写真实 candidate 业务
不接数字人
```

### STOP 条件

Schema V1 和基础存取测试 PASS。

**立即停止。**

---

## Task 1.4｜LangGraph SQLite Checkpointer

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 1.4｜LangGraph SQLite Checkpointer。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 1.4 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 1.4，避免重复实现。
4. 只修改 Task 1.4 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 1.4 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 1.4。
```

### 目标

只建立 thread 级持久化。

### 允许改动范围

```text
agent_service/runtime/checkpointer.py
agent_service/db/checkpoints.sqlite 运行时文件
相关测试
```

### 具体步骤

1. 初始化 `AsyncSqliteSaver` 或当前官方推荐 SQLite saver。
2. thread key 使用 `session_id`。
3. 写最小测试 graph / agent。
4. 同 thread 两轮消息能恢复。
5. 关闭并重新初始化 checkpointer 后仍能读到 thread state。

### 验收

必须有自动测试证明：

```text
thread A 的历史存在
thread B 不串数据
重新创建进程级对象后 thread A 可恢复
```

### 本 Task 不做

```text
不接 Business DB 状态
不做 long-term memory
不写 Supervisor 业务 prompt
```

### STOP 条件

Checkpointer 独立持久化测试 PASS。

**立即停止。**

---

## Task 1.5｜ContentAgentState 与 ContentAppContext

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 1.5｜ContentAgentState 与 ContentAppContext。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 1.5 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 1.5，避免重复实现。
4. 只修改 Task 1.5 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 1.5 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 1.5。
```

### 目标

把“thread state”和“业务数据库对象”在代码层彻底分开。

### 允许改动范围

```text
agent_service/runtime/context.py
agent_service/schemas/state.py
相关测试
```

### ContentAgentState 只允许放

```text
messages
session_id / task_id pointer
current_candidate_batch_id
current_topic_id
current_draft_id
current_draft_version
active_review_id pointer（先可为空）
generation_id pointer
少量运行计数器
```

### 禁止放入 Agent State

```text
完整 draft 历史
完整 research 历史
完整 generation authorization 对象
完整 content memory
```

### ContentAppContext 放

```text
request_id
session_id
stores / service handles
Go capability client
config
```

### 验收

1. 类型定义通过。
2. 测试能序列化 state。
3. 代码注释明确 Business DB 是 canonical truth。

### STOP 条件

两种 State 的边界已经在类型层固定。

**立即停止。**

---

## Task 1.6｜Model Factory

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 1.6｜Model Factory。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 1.6 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 1.6，避免重复实现。
4. 只修改 Task 1.6 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 1.6 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 1.6。
```

### 目标

建立统一模型创建入口，不实现 Agent。

### 允许改动范围

```text
agent_service/runtime/model_factory.py
agent_service/config*
相关测试
```

### 具体步骤

1. 支持至少 supervisor model 配置。
2. 支持 Script DeepSeek 路线的配置位。
3. model 不能散落在各 Agent 文件硬编码。
4. 环境变量缺失要给明确错误。
5. 写 mock / config test，不要求真实付费调用覆盖所有 provider。

### 验收

```text
get_model("supervisor")
get_model("script")
```

能按配置返回预期实例或明确配置错误。

### STOP 条件

模型创建入口统一完成。

**立即停止。**

---

## Task 1.7｜最小 Supervisor 连续对话 Smoke Test

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 1.7｜最小 Supervisor 连续对话 Smoke Test。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 1.7 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 1.7，避免重复实现。
4. 只修改 Task 1.7 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 1.7 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 1.7。
```

### 目标

第一次真正创建 `create_agent`，但只有一个无业务 Tool 的最小 Supervisor。

### 允许改动范围

```text
agent_service/agents/supervisor.py
agent_service/runtime/* 必要连接点
agent_service/app.py
测试
```

### 具体步骤

1. 用 `create_agent` 创建 Supervisor。
2. 接入 checkpointer。
3. 绑定 `thread_id=session_id`。
4. 暴露最小 `/internal/agent/chat`。
5. 同一个 session 连续发送两轮。
6. 重启 Agent Service 后继续第三轮。

### 验收

必须证明：

```text
同 session 能理解前一轮
不同 session 不串历史
重启后历史仍在
```

此时内容回答质量不重要。

### 本 Task 不做

```text
不加 Topic Agent
不加 Script Agent
不加 Search Tool
```

### STOP 条件

最小 Agent thread 闭环跑通。

**立即停止。**

---

## Task 1.8｜Go → Python 最小 Agent Proxy

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 1.8｜Go → Python 最小 Agent Proxy。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 1.8 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 1.8，避免重复实现。
4. 只修改 Task 1.8 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 1.8 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 1.8。
```

### 目标

让 Browser 未来仍然只需要访问 Go。

### 允许改动范围

```text
Phase 0 确定的 Go router / handler
新增 internal/agentproxy/（如契约采用该目录）
必要配置
代理相关测试
```

### 具体步骤

1. 按 `internal-api-contract.md` 增加 Go public agent chat endpoint。
2. Go 只做代理，不做 Agent 判断。
3. 透传 request_id / session_id。
4. Python 错误转为统一 Go API 错误。
5. 保持旧页面 / 旧 API 不受影响。

### 验收

```text
Browser/curl → Go endpoint → Python Supervisor → 返回结果
```

旧核心接口 smoke test 仍通过。

### STOP 条件

最小跨语言调用链完成。

**Phase 1 到此结束，停止。**

---

# Phase 2｜最小完整业务薄切片

**Phase 目标：** 只跑通：

```text
选题 → 第三个 → 写稿 → 局部改稿 → 明确授权 → 数字人提交
```

Research、Review、长期 Memory 暂时都不实现。

---

## Task 2.1｜Topic Schema + Go Search Client

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 2.1｜Topic Schema + Go Search Client。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 2.1 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 2.1，避免重复实现。
4. 只修改 Task 2.1 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 2.1 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 2.1。
```

### 目标

为 Topic Agent 准备结构化结果与最小搜索 Tool。

### 允许改动范围

```text
agent_service/schemas/topic.py
agent_service/tools/go_search.py
Go internal search adapter（仅 Phase 0 判定需要时）
契约测试
```

### 具体步骤

1. 实现 `TopicCandidate / TopicResult`。
2. topic_id 由业务层生成，不让模型自由作为数据库主键。
3. 实现 Python → Go search client。
4. 统一 timeout / error object。
5. Tool 返回数据，不做“推荐哪个”判断。

### 验收

mock 或真实调用能得到可解析搜索结果；错误能区分 retryable。

### STOP 条件

Topic Agent 所需 Schema + Search Tool 可独立调用。

**立即停止。**

---

## Task 2.2｜Topic Agent

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 2.2｜Topic Agent。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 2.2 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 2.2，避免重复实现。
4. 只修改 Task 2.2 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 2.2 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 2.2。
```

### 目标

只实现专业 Topic Agent，不接 Supervisor。

### 允许改动范围

```text
agent_service/agents/topic.py
agent_service/principles/topic.md
必要的 Topic tests
```

### 具体步骤

1. `create_agent` 创建 Topic Agent。
2. 只挂 Topic 必需 Tools。
3. 使用结构化 `response_format=TopicResult`。
4. Prompt 明确：事实不足必须标 uncertainty。
5. 独立测试“今天做啥好呢”。

### 验收

Topic Agent 独立返回 3～5 个合法候选；结构化解析稳定。

### 本 Task 不做

```text
不做 candidate batch 落库
不做“第三个”
不挂 Supervisor
```

### STOP 条件

Topic Agent 独立能力 PASS。

**立即停止。**

---

## Task 2.3｜Candidate Batch 持久化与展示映射

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 2.3｜Candidate Batch 持久化与展示映射。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 2.3 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 2.3，避免重复实现。
4. 只修改 Task 2.3 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 2.3 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 2.3。
```

### 目标

解决“用户看到的第三个到底是哪一个”。

### 允许改动范围

```text
agent_service/services/task_store.py
candidate batch 相关 schema / repository
tests
```

### 具体步骤

1. 保存 candidate_batch_id。
2. 每个候选保存稳定 topic_id。
3. 保存 `display_index`。
4. 支持前端顺序与原始生成顺序不同。
5. 提供 `get_candidate_by_display_index(batch_id, index)`。

### 验收

测试场景：

```text
原始 A/B/C
展示 B/C/A
“第三个”解析为 A
```

必须 PASS。

### STOP 条件

展示顺序不再依赖数组隐式位置。

**立即停止。**

---

## Task 2.4｜Supervisor Topic Tool Wrapper + 指代解析最小闭环

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 2.4｜Supervisor Topic Tool Wrapper + 指代解析最小闭环。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 2.4 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 2.4，避免重复实现。
4. 只修改 Task 2.4 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 2.4 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 2.4。
```

### 目标

把 Topic Agent 作为 subagent tool 挂给 Supervisor，并支持当前候选中的序号指代。

### 允许改动范围

```text
agent_service/agent_tools/topic_expert.py
agent_service/agents/supervisor.py
state pointer 更新代码
相关测试
```

### 具体步骤

1. wrapper 只传必要任务描述。
2. Topic Agent 返回后落 candidate batch。
3. Supervisor state 只保存 current batch pointer。
4. 用户说“第三个”时通过业务 resolver 获取稳定 topic_id。
5. 本 Task 只需要能选中，不写稿。

### 验收

连续对话：

```text
今天做啥好呢
→ 返回候选
第三个不错
→ selected / confirmed topic 指向正确对象
```

### STOP 条件

Topic 子 Agent + 当前候选指代闭环完成。

**立即停止。不要开始写稿。**

---

## Task 2.5｜Draft Store 与版本模型

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 2.5｜Draft Store 与版本模型。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 2.5 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 2.5，避免重复实现。
4. 只修改 Task 2.5 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 2.5 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 2.5。
```

### 目标

先把稿件版本的确定性规则实现，再接 Script Agent。

### 允许改动范围

```text
agent_service/schemas/draft.py
agent_service/services/task_store.py
agent_service/tools/drafts.py
相关 tests
```

### 具体步骤

1. 定义 Draft / DraftVersion 数据模型。
2. 创建稿件时 v1。
3. 修改时 version + 1。
4. 旧版本永不覆盖。
5. current_draft pointer 只有 commit 成功后更新。
6. 支持读取 current / specific version。

### 验收

测试：

```text
create → v1
edit → v2
v1 仍存在
current=v2
重复 version 被 DB 拒绝
```

### STOP 条件

版本存储本身可靠，不依赖 LLM。

**立即停止。**

---

## Task 2.6｜Script Agent 独立生成能力

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 2.6｜Script Agent 独立生成能力。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 2.6 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 2.6，避免重复实现。
4. 只修改 Task 2.6 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 2.6 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 2.6。
```

### 目标

实现 Script Agent 的“生成一版稿件”，暂不处理局部改稿。

### 允许改动范围

```text
agent_service/agents/script.py
agent_service/principles/script.md
agent_service/schemas/draft.py 必要补充
相关 tests
```

### 具体步骤

1. 按 Phase 0 决策选择 DeepSeek direct 或旧 Go writing tool。
2. 输入 confirmed topic + 必要事实。
3. 输出结构化 draft 内容。
4. 明确 hook/body/cta 与完整 script 的生成规则。
5. 独立测试。

### 验收

给固定 topic，能稳定返回可保存的 Draft payload。

### 本 Task 不做

```text
不保存版本
不做局部修改
不接 Supervisor
```

### STOP 条件

Script Agent 独立写稿 PASS。

**立即停止。**

---

## Task 2.7｜Script Tool Wrapper + 生成 v1

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 2.7｜Script Tool Wrapper + 生成 v1。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 2.7 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 2.7，避免重复实现。
4. 只修改 Task 2.7 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 2.7 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 2.7。
```

### 目标

让“第三个写吧”真正生成并落库 v1。

### 允许改动范围

```text
agent_service/agent_tools/script_expert.py
agent_service/agents/supervisor.py
Draft store 接入点
tests
```

### 具体步骤

1. Supervisor 确认 topic。
2. 调 `script_expert`。
3. Script Agent 只返回 payload。
4. wrapper / service 落库。
5. commit 后更新 current_draft pointer。
6. 返回用户稿件。

### 验收

```text
今天做啥
→ 第三个写吧
→ 数据库出现 draft v1
→ state pointer 指向 v1
→ 用户看到的文本与 v1 一致
```

### STOP 条件

“选题 → 写稿 v1”闭环完成。

**立即停止。**

---

## Task 2.8｜局部改稿 + 版本失效规则

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 2.8｜局部改稿 + 版本失效规则。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 2.8 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 2.8，避免重复实现。
4. 只修改 Task 2.8 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 2.8 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 2.8。
```

### 目标

实现：

```text
第二段短一点
开头换成问句
```

并保证只创建新版本。

### 允许改动范围

```text
Script Agent / wrapper
Draft service
state invalidation service（如需新增）
tests
```

### 具体步骤

1. 读取 current draft。
2. 把原稿和修改指令传给 Script Agent。
3. 只修改目标部分；由 Prompt 约束。
4. 保存 v2。
5. old review pointer 清空（即使 Review 尚未实现，也留接口）。
6. old generation authorization 失效。
7. commit 后 current=v2。

### 验收

连续修改：

```text
v1 → v2 → v3
```

且 v1/v2 均可读取。

### STOP 条件

局部改稿版本链与失效规则 PASS。

**立即停止。**

---

## Task 2.9｜Generation Authorization

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 2.9｜Generation Authorization。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 2.9 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 2.9，避免重复实现。
4. 只修改 Task 2.9 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 2.9 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 2.9。
```

### 目标

只实现业务授权，不调用数字人。

### 允许改动范围

```text
agent_service/schemas/generation.py
agent_service/services/authorization_service.py
Supervisor 生成意图解析的最小连接点
tests
```

### 具体规则

```text
“生成吧” → 明确授权
“可以生成” → 明确授权
“就这版” → 只有上一轮语义明确在询问是否生成时才授权
“这版可以” → 默认只是稿件确认，不自动生成
```

Authorization 必须绑定：

```text
task_id
draft_id
draft_version
params_hash
created_at
status
```

### 验收

测试至少覆盖：

```text
v2 授权有效
改成 v3 后 v2 授权不能用于 v3
“这版可以”不直接触发 generation
```

### STOP 条件

授权对象和语义判断完成，但系统尚未提交数字人。

**立即停止。**

---

## Task 2.10｜Digital Human Tool + 幂等提交

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 2.10｜Digital Human Tool + 幂等提交。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 2.10 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 2.10，避免重复实现。
4. 只修改 Task 2.10 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 2.10 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 2.10。
```

### 目标

将已有数字人能力封装为 side-effect Tool。

### 允许改动范围

```text
agent_service/tools/digital_human.py
agent_service/services/generation_service.py
必要 Go internal adapter
generation repository
tests
```

### 固定执行顺序

```text
校验 authorization
→ 校验 draft/version/params
→ 计算 idempotency_key
→ 查已有 generation
→ 没有才 submit
→ 保存 provider_task_id
```

### 验收

必须测试：

```text
同一授权调用两次
→ provider submit 只发生一次
```

provider timeout unknown 时：

```text
状态 = unknown_submission
不得盲目二次 submit
```

### STOP 条件

数字人 Tool 本身幂等可靠。

**立即停止。**

---

## Task 2.11｜Phase 2 E2E 薄切片验收

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 2.11｜Phase 2 E2E 薄切片验收。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 2.11 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 2.11，避免重复实现。
4. 只修改 Task 2.11 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 2.11 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 2.11。
```

### 目标

不新增功能，只把 Phase 2 已完成模块串起来并修复**仅属于 Phase 2 范围**的问题。

### 允许改动范围

仅限 Phase 2 已经创建的文件。

### 必测对话

```text
今天做啥好呢
第三个写吧
第二段短一点
这版可以
生成了吗？        # 此处不能擅自 submit
生成吧
```

### 验收

必须同时证明：

```text
候选序号正确
v1/v2 保留
“这版可以”未生成
“生成吧”绑定 v2
重复请求不重复 submit
重启后 thread 与 current draft 可恢复
```

### STOP 条件

Phase 2 E2E PASS。

**Phase 2 到此结束。不要开始 Research。**

---

# Phase 3｜Research 与可追溯事实

---

## Task 3.1｜Research Schema + DB 表

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 3.1｜Research Schema + DB 表。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 3.1 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 3.1，避免重复实现。
4. 只修改 Task 3.1 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 3.1 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 3.1。
```

### 目标

建立事实与来源一一可追溯的数据结构。

### 允许改动范围

```text
agent_service/schemas/research.py
DB migration/schema
research repository
tests
```

### 数据要求

每条 Fact 至少能关联：

```text
fact_id
statement
source_ids
verified_at
status
valid_until（可空）
```

Source 至少：

```text
source_id
title
url
source_type
published_at
fetched_at
```

### 验收

一个 ResearchResult 能明确回答“哪条来源支持哪条事实”。

### STOP 条件

Research 数据层完成。

**立即停止。**

---

## Task 3.2｜Research Search / Fetch Tools

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 3.2｜Research Search / Fetch Tools。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 3.2 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 3.2，避免重复实现。
4. 只修改 Task 3.2 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 3.2 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 3.2。
```

### 目标

实现 Research Agent 可使用的事实工具，不写 Agent。

### 允许改动范围

```text
agent_service/tools/go_search.py
agent_service/tools/source_fetch.py（如需要）
必要 Go adapter
tests
```

### 验收

Tools 能返回规范化 Source 数据；404 / timeout / blocked 明确区分。

### STOP 条件

Research Tool 层完成。

**立即停止。**

---

## Task 3.3｜Research Agent

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 3.3｜Research Agent。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 3.3 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 3.3，避免重复实现。
4. 只修改 Task 3.3 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 3.3 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 3.3。
```

### 目标

实现独立 Research Agent。

### 允许改动范围

```text
agent_service/agents/research.py
agent_service/principles/research.md
tests
```

### 规则

```text
官方来源优先
无法确认 = unknown
二手信息可作为线索，不自动升级为 verified
```

### 验收

固定问题：

```text
“这个岗位现在还招吗？”
```

能返回 verified / unknown，而不是强行肯定。

### STOP 条件

Research Agent 独立 PASS。

**立即停止。**

---

## Task 3.4｜招聘状态判定服务

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 3.4｜招聘状态判定服务。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 3.4 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 3.4，避免重复实现。
4. 只修改 Task 3.4 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 3.4 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 3.4。
```

### 目标

把 open / closed / unknown 的硬规则从 Prompt 抽到程序可复核层。

### 允许改动范围

```text
research service / schema / tests
```

### 规则至少覆盖

```text
明确未来截止时间 → open（满足其他条件时）
明确已截止 → closed
页面存在但无可判断时间 → unknown
官方页面不可访问且无可靠替代证据 → unknown
来源冲突 → unknown + conflict
```

### 验收

表驱动测试覆盖上述情况。

### STOP 条件

招聘状态判断可重复、可测试。

**立即停止。**

---

## Task 3.5｜Research 接入 Supervisor

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 3.5｜Research 接入 Supervisor。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 3.5 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 3.5，避免重复实现。
4. 只修改 Task 3.5 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 3.5 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 3.5。
```

### 目标

增加 `research_expert` subagent Tool，并让 Supervisor 在需要事实核实时调用。

### 允许改动范围

```text
agent_tools/research_expert.py
supervisor.py
Research state pointer / service
tests
```

### 必测

```text
“第三个现在还招吗？” → Research
“第三个写一版” → 不应先强制 Research（除非信息缺失达到规则）
```

### STOP 条件

Supervisor 的 Research 路由与事实落库 PASS。

**Phase 3 结束。**

---

# Phase 4｜Content Review 与版本绑定

---

## Task 4.1｜Review Schema + DB

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 4.1｜Review Schema + DB。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 4.1 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 4.1，避免重复实现。
4. 只修改 Task 4.1 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 4.1 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 4.1。
```

### 目标

让审核结果严格绑定稿件版本。

### 允许改动范围

```text
schemas/review.py
DB schema / migration
review repository
tests
```

### 必须字段

```text
review_id
draft_id
draft_version
status
issues
must_fix
risk_items
ready_for_generation
created_at
```

### STOP 条件

Review 数据层能拒绝“无 draft/version”的审核记录。

**立即停止。**

---

## Task 4.2｜Content Review Agent

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 4.2｜Content Review Agent。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 4.2 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 4.2，避免重复实现。
4. 只修改 Task 4.2 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 4.2 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 4.2。
```

### 目标

实现 Review Agent 独立能力。

### 允许改动范围

```text
agents/review.py
principles/content_review.md
tests
```

### 输出

结构化 `ReviewResult`。

### 验收

固定稿件能得到稳定结构化审核结果。

### STOP 条件

Review Agent 独立 PASS。

**立即停止。**

---

## Task 4.3｜Review 版本失效规则

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 4.3｜Review 版本失效规则。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 4.3 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 4.3，避免重复实现。
4. 只修改 Task 4.3 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 4.3 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 4.3。
```

### 目标

确保：

```text
v2 review pass
→ 用户改稿生成 v3
→ v2 review 仍保留历史
→ current v3 不得显示“已通过”
```

### 允许改动范围

```text
Draft / Review service
state pointer
tests
```

### STOP 条件

版本失效自动测试 PASS。

**立即停止。**

---

## Task 4.4｜Review 接入 Supervisor

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 4.4｜Review 接入 Supervisor。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 4.4 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 4.4，避免重复实现。
4. 只修改 Task 4.4 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 4.4 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 4.4。
```

### 目标

增加 `content_reviewer` tool wrapper。

### 必测

```text
“这个稿有没有风险？” → Review
“第二段短一点” → Script，不应调用 Review 代替改稿
```

`need_research` 时允许 Supervisor 再决定是否 Research，但限制在调用上限内。

### STOP 条件

Review 路由与版本绑定 PASS。

**Phase 4 结束。**

---

# Phase 5｜Long-term Content Memory MVP

---

## Task 5.1｜长期内容表 Schema

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 5.1｜长期内容表 Schema。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 5.1 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 5.1，避免重复实现。
4. 只修改 Task 5.1 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 5.1 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 5.1。
```

### 目标

只创建第一版长期资产表。

### 表

```text
content_items
topic_history
revision_history
content_metrics
```

如 V6 前文定义的 `content_learnings` 尚无真实写入来源，本 Task 不强制创建。

### 验收

Schema / migration / repository tests PASS。

### STOP 条件

长期 Memory 数据层完成。

**立即停止。**

---

## Task 5.2｜Memory 读取 Tools

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 5.2｜Memory 读取 Tools。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 5.2 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 5.2，避免重复实现。
4. 只修改 Task 5.2 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 5.2 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 5.2。
```

### 目标

实现：

```text
get_recent_topics
get_similar_scripts
get_revision_history
```

### 规则

Tools 返回历史事实，不自己判断“这个选题值得做”。

### 验收

按 account / 时间 / topic 过滤测试 PASS。

### STOP 条件

读取 Tool 可独立使用。

**立即停止。**

---

## Task 5.3｜Memory 写入 Tools

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 5.3｜Memory 写入 Tools。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 5.3 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 5.3，避免重复实现。
4. 只修改 Task 5.3 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 5.3 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 5.3。
```

### 目标

实现：

```text
save_content_item
save_revision_record
update_content_metrics
```

### 写入原则

```text
临时候选不进长期库
未采用草稿不作为最终 content_item
明确用户修改可以进入 revision_history
最终采用稿 / 已生成 / 已发布内容才进入稳定资产
```

### 验收

错误写入场景有测试保护。

### STOP 条件

长期写入边界 PASS。

**立即停止。**

---

## Task 5.4｜Topic / Script 接入 Memory

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 5.4｜Topic / Script 接入 Memory。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 5.4 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 5.4，避免重复实现。
4. 只修改 Task 5.4 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 5.4 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 5.4。
```

### 目标

只让：

```text
Topic Agent → 查询最近选题
Script Agent → 查询相似稿件 / 修改历史
```

### 允许改动范围

对应两个 Agent、wrapper、Memory Tools。

### 验收

Trace 能看到只有需要时才调用 Memory Tool，不是每轮机械全查。

### STOP 条件

两个 Agent 的长期记忆读取可用。

**立即停止。**

---

## Task 5.5｜跨 Thread Memory 验收

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 5.5｜跨 Thread Memory 验收。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 5.5 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 5.5，避免重复实现。
4. 只修改 Task 5.5 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 5.5 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 5.5。
```

### 目标

不新增功能，只验证：

```text
thread A 产生最终稿
thread B 新会话
→ 仍可检索 A 的长期内容资产
→ 但不能继承 A 的当前 task state
```

### STOP 条件

短期 thread state 与长期 memory 隔离测试 PASS。

**Phase 5 结束。**

---

# Phase 6｜Streaming、事件、限制与 Trace

---

## Task 6.1｜统一 Event Schema

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 6.1｜统一 Event Schema。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 6.1 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 6.1，避免重复实现。
4. 只修改 Task 6.1 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 6.1 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 6.1。
```

### 目标

先定义事件，不做 SSE。

### 事件至少

```text
agent_started
agent_finished
tool_started
tool_finished
candidate_batch_updated
draft_updated
review_updated
waiting_user
generation_started
generation_completed
error
```

### 字段至少

```text
event_id
request_id
session_id
task_id
type
timestamp
payload
```

### STOP 条件

Event schema 与序列化测试 PASS。

**立即停止。**

---

## Task 6.2｜Python Streaming / Event Bus

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 6.2｜Python Streaming / Event Bus。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 6.2 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 6.2，避免重复实现。
4. 只修改 Task 6.2 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 6.2 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 6.2。
```

### 目标

把 Agent `.astream()` 与业务 custom events 转成统一内部流。

### 允许改动范围

```text
runtime/streaming.py
services/event_bus.py
Python chat stream endpoint
tests
```

### 验收

curl 连接 Python stream endpoint，能看到至少：

```text
agent_started
一段 message chunk
agent_finished
```

### STOP 条件

Python 内部 streaming 闭环完成。

**立即停止。**

---

## Task 6.3｜Go SSE Proxy

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 6.3｜Go SSE Proxy。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 6.3 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 6.3，避免重复实现。
4. 只修改 Task 6.3 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 6.3 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 6.3。
```

### 目标

让 Browser 通过 Go 接收 Python stream。

### 允许改动范围

Go agent proxy / stream handler。

### 验收

```text
Browser/curl → Go SSE → Python events
```

断连后资源能正确释放。

### STOP 条件

跨语言 streaming PASS。

**立即停止。**

---

## Task 6.4｜调用上限、timeout 与错误 Middleware

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 6.4｜调用上限、timeout 与错误 Middleware。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 6.4 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 6.4，避免重复实现。
4. 只修改 Task 6.4 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 6.4 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 6.4。
```

### 目标

落地 63.5 的运行上限，而不是只写文档。

### 实现

```text
recursion_limit
specialist_call_count
same_specialist_call_count
research_round_count
Tool timeout
retryable error 限制
```

### 验收

人为制造循环 / timeout，系统能停下来并返回已知信息 + uncertainty。

### STOP 条件

不会通过无限循环消耗运行时间。

**立即停止。**

---

## Task 6.5｜LangSmith Trace + Local Business Trace

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 6.5｜LangSmith Trace + Local Business Trace。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 6.5 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 6.5，避免重复实现。
4. 只修改 Task 6.5 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 6.5 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 6.5。
```

### 目标

同时建立两类可观测性。

### LangSmith 记录

```text
model
subagent
Tool
latency
```

### Local trace 记录

```text
request_id
session_id
task_id
draft version change
authorization
generation
error_code
```

### 验收

跑一条完整 Phase 2 Case，可以同时在：

```text
LangSmith Trace
本地 business trace
```

定位关键步骤。

### STOP 条件

Trace 可用于调试和作品集截图。

**Phase 6 结束。**

---

# Phase 7｜是否需要 Custom StateGraph 的决策门

---

## Task 7.1｜Graph Necessity Review

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 7.1｜Graph Necessity Review。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 7.1 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 7.1，避免重复实现。
4. 只修改 Task 7.1 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 7.1 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 7.1。
```

### 目标

**只评估，不编码。**

回看真实 Trace，检查是否已经出现：

```text
A. Research 多来源并行 + 汇总
B. 稳定的补证据循环
C. 必须 interrupt/resume 的子流程
D. 纯 Agent 路由导致明显不可控的确定性流程
```

### 允许改动范围

只新增：

```text
docs/custom-graph-decision.md
```

### 结论只能二选一

```text
NO_GRAPH_NEEDED
```

或：

```text
GRAPH_NEEDED_FOR: <具体局部流程>
```

不能写“以后可能”。

### STOP 条件

决策文档完成。

**立即停止。**

---

## Task 7.2｜局部 Custom StateGraph（条件任务）

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 7.2｜局部 Custom StateGraph（条件任务）。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 7.2 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 7.2，避免重复实现。
4. 只修改 Task 7.2 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 7.2 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 7.2。
```

### 执行前提

只有 Task 7.1 明确输出：

```text
GRAPH_NEEDED_FOR: ...
```

才执行。

如果是 `NO_GRAPH_NEEDED`：

```text
直接 SKIP Task 7.2
进入 Phase 8
```

### 目标

只把被证明需要 deterministic workflow 的**那一个局部流程**实现为 StateGraph。

### 禁止

```text
不得把整个系统改成 Topic→Research→Script→Review 固定图
不得重写 Supervisor 主架构
```

### STOP 条件

该局部 graph 有独立测试，并作为 Tool / 子流程重新接回 Supervisor。

**停止。**

---

# Phase 8｜前端产品化

前端任务必须在后端接口已稳定后开始。

---

## Task 8.1｜聊天 API Client + 基础聊天框

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 8.1｜聊天 API Client + 基础聊天框。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 8.1 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 8.1，避免重复实现。
4. 只修改 Task 8.1 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 8.1 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 8.1。
```

### 目标

只完成：

```text
发送消息
显示用户消息
显示 Assistant 文本
保持 session_id
```

### 禁止

```text
不做选题卡片
不做小牛马动画
不做复杂右栏
```

### 验收

浏览器能完整进行基础连续聊天。

### STOP 条件

聊天壳完成。

**立即停止。**

---

## Task 8.2｜结构化选题卡片

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 8.2｜结构化选题卡片。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 8.2 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 8.2，避免重复实现。
4. 只修改 Task 8.2 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 8.2 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 8.2。
```

### 目标

把 candidate batch 作为结构化 UI 渲染，不再依赖 Assistant 文本里的 1/2/3。

### 必须保存

```text
candidate_batch_id
topic_id
display_index
```

点击卡片与说“第三个”应指向同一对象。

### STOP 条件

Topic cards 与 display mapping 一致性测试 PASS。

**立即停止。**

---

## Task 8.3｜当前任务 / Draft 右侧面板

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 8.3｜当前任务 / Draft 右侧面板。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 8.3 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 8.3，避免重复实现。
4. 只修改 Task 8.3 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 8.3 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 8.3。
```

### 目标

显示：

```text
当前 topic
current draft version
稿件正文
review status（如有）
generation status
```

### 禁止

不做动画。

### STOP 条件

刷新页面后右侧状态可从后端恢复。

**立即停止。**

---

## Task 8.4｜Agent 工作状态与“小牛马”事件驱动

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 8.4｜Agent 工作状态与“小牛马”事件驱动。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 8.4 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 8.4，避免重复实现。
4. 只修改 Task 8.4 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 8.4 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 8.4。
```

### 目标

只消费 Phase 6 的事件，不从文本猜状态。

### 映射

```text
agent_started research → Research working
agent_finished research → Research done
tool_started search → 搜索动画
generation_started → 数字人 working
```

### STOP 条件

至少 Topic / Research / Script / Review / Generation 五类状态事件可视化。

**立即停止。**

---

## Task 8.5｜数字人结果 + 页面恢复

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 8.5｜数字人结果 + 页面恢复。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 8.5 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 8.5，避免重复实现。
4. 只修改 Task 8.5 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 8.5 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 8.5。
```

### 目标

显示 generation 状态、视频结果，并验证刷新后不丢。

### 验收

```text
pending → processing → completed
刷新页面
→ 仍能读取对应 generation
```

### STOP 条件

完整用户可见链路完成。

**Phase 8 结束。**

---

# Phase 9｜测试、故障注入与作品集冻结

---

## Task 9.1｜单元测试与 Contract Test 补齐

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 9.1｜单元测试与 Contract Test 补齐。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 9.1 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 9.1，避免重复实现。
4. 只修改 Task 9.1 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 9.1 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 9.1。
```

### 目标

盘点前面每个 Task 已有测试，只补明显空缺。

### 必须覆盖

```text
candidate display mapping
draft version
authorization invalidation
generation idempotency
Research status
Review version binding
Memory thread isolation
```

### STOP 条件

核心 domain tests 全绿。

**立即停止。**

---

## Task 9.2｜并发与串行 Session 测试

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 9.2｜并发与串行 Session 测试。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 9.2 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 9.2，避免重复实现。
4. 只修改 Task 9.2 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 9.2 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 9.2。
```

### 目标

专门测试两个快速连续修改请求。

### Case

```text
current=v1
并发消息 A：第二段短一点
并发消息 B：开头换问句
```

### 期望

```text
v1 → v2 → v3
```

不得产生两个互相覆盖的 v2。

### STOP 条件

并发测试稳定 PASS。

**立即停止。**

---

## Task 9.3｜故障注入

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 9.3｜故障注入。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 9.3 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 9.3，避免重复实现。
4. 只修改 Task 9.3 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 9.3 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 9.3。
```

### 目标

按 Case 逐个模拟，不做新功能。

### 必测

```text
search 0 result
source 404
search timeout
Research unknown
DB commit failure
provider submit timeout
provider unknown_submission
Specialist 调用达到上限
recursion_limit
```

### STOP 条件

每个故障都有明确、非误导性的用户响应和业务状态。

**立即停止。**

---

## Task 9.4｜进程重启恢复

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 9.4｜进程重启恢复。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 9.4 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 9.4，避免重复实现。
4. 只修改 Task 9.4 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 9.4 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 9.4。
```

### 目标

专门验证：

```text
Python 重启
Go 重启
两者都重启
```

之后：

```text
thread history
current task
current draft
generation provider_task_id
```

仍可恢复。

### STOP 条件

恢复测试 PASS。

**立即停止。**

---

## Task 9.5｜最终 E2E Case 冻结

### 本 Task 可复制执行 Prompt

```text
读取本项目唯一执行说明：AI数字人内容生产Multi-Agent_LangChain-LangGraph_开发文档_V6.1_唯一执行入口版.md。

现在只执行 Task 9.5｜最终 E2E Case 冻结。

强制要求：
1. 先阅读 61.0 全局执行协议和 Task 9.5 的完整任务卡片。
2. 只读取本 Task “前置条件 / 前置产物”明确要求的仓库文件，以及为理解这些文件所必需的直接依赖；不要浏览无关模块。
3. 开始修改前，先检查当前仓库是否已经部分或全部完成 Task 9.5，避免重复实现。
4. 只修改 Task 9.5 “允许改动范围”内的文件；如必须越界，标记 BLOCKED 并停止，不得自行扩大范围。
5. 严格完成“具体步骤”，但不要实现该 Task 明确排除的内容，也不要提前实现下一 Task。
6. 必须执行 Task 9.5 规定的全部验收；没有通过验收不能报告 DONE。
7. 如果测试失败原因属于后续 Task、架构冲突、外部依赖不可用或需要超出允许范围的修改，按 61.0.4 输出 BLOCKED。
8. 完成后按 61.0.3 固定格式输出 Task Report。
9. 输出 Task Report 后立即 STOP。
10. 绝对不要开始下一个 Task，即使你认为顺手完成会更快。

本轮唯一目标：完成并验收 Task 9.5。
```

### 目标

只跑最终演示脚本，不再继续扩需求。

### 最终 Case A｜正常链路

```text
今天做啥好呢
第三个不错，但帮我看看现在还招不招
可以，写吧
第二段短一点
看看有没有风险
这个版本可以
生成吧
```

### 最终 Case B｜不确定事实

```text
这个岗位现在还招吗？
```

证据不足时必须回答无法确认。

### 最终 Case C｜恢复

中途重启服务后继续：

```text
刚才那版继续改一下
```

### 最终 Case D｜幂等

重复发送生成请求，不产生两个 provider task。

### 产物

新增：

```text
docs/demo-script.md
docs/final-acceptance-report.md
```

### STOP 条件

最终验收报告完成。

**项目 MVP 到此冻结。不要继续“顺便优化”。**

---

# 61.10 推荐实际执行顺序总表

```text
Phase 0
0.1 → STOP
0.2 → STOP
0.3 → STOP
0.4 → STOP
0.5 → STOP
0.6 → STOP

Phase 1
1.1 → STOP
1.2 → STOP
1.3 → STOP
1.4 → STOP
1.5 → STOP
1.6 → STOP
1.7 → STOP
1.8 → STOP

Phase 2
2.1 → STOP
2.2 → STOP
2.3 → STOP
2.4 → STOP
2.5 → STOP
2.6 → STOP
2.7 → STOP
2.8 → STOP
2.9 → STOP
2.10 → STOP
2.11 → STOP

Phase 3
3.1 → STOP
3.2 → STOP
3.3 → STOP
3.4 → STOP
3.5 → STOP

Phase 4
4.1 → STOP
4.2 → STOP
4.3 → STOP
4.4 → STOP

Phase 5
5.1 → STOP
5.2 → STOP
5.3 → STOP
5.4 → STOP
5.5 → STOP

Phase 6
6.1 → STOP
6.2 → STOP
6.3 → STOP
6.4 → STOP
6.5 → STOP

Phase 7
7.1 → STOP
7.2 → STOP / SKIP

Phase 8
8.1 → STOP
8.2 → STOP
8.3 → STOP
8.4 → STOP
8.5 → STOP

Phase 9
9.1 → STOP
9.2 → STOP
9.3 → STOP
9.4 → STOP
9.5 → MVP FREEZE
```

这张表不是要求一次性跑完，而是要求：

```text
一次 Codex 会话 / 一次明确执行请求
≈ 一个 Task
```

如果某 Task 很轻，可以由用户**主动**一次指定两个 Task；模型自己不得因为“还有时间”继续执行下一项。

表中每一格“→ STOP”是**单 Task 模式**（61.0.1）的默认边界。用户在指令里显式写出“允许按阶段执行”或“Phase 模式”时，边界改为：

```text
一次明确执行请求
≈ 一个 Phase（Phase 内每个 Task 仍各自验收、各自提交）
→ Phase 末 STOP
```

Phase 模式不改变表中任何一格的实际工作量与验收标准，只改变停下来汇报的位置；判定规则见 61.0.1a。

---

# 61.11 Task 开始前的状态检查

每一轮执行新 Task 前先检查：

```text
上一 Task 是否 DONE
上一 Task 的 tests 是否仍然 PASS
本 Task 前置产物是否存在
工作区是否存在未解释的冲突修改
```

如果前置 Task 未完成：

```text
不要跳着实现
返回 BLOCKED
STOP
```

但以下任务允许明确跳过：

```text
Task 7.2
```

仅当 Task 7.1 输出 `NO_GRAPH_NEEDED`。

---

# 61.12 为什么这样拆

这些 Task 的边界刻意按照“工程不变量”拆，而不是按照“模型能一次写多少代码”拆。

例如：

```text
先做 Draft Store
再做 Script Agent 接入
```

是因为：

```text
版本号必须由程序控制
不能让 Script Agent 同时决定版本和写数据库
```

又例如：

```text
先做 Authorization
再做 Digital Human Tool
```

是因为：

```text
要先证明“谁被授权生成”
再允许 side effect 真正发生
```

再例如：

```text
先定义 Event Schema
再做 SSE
```

是为了避免前后端在事件字段还没稳定时同时返工。

因此即使 GPT-6 一轮看起来“还能继续”，也不应该自动跨过 STOP Gate。

---

# 62. MVP Case

至少跑通：

---

## Case 1

```text
用户：
今天做啥好呢
```

系统：

```text
自主调用 Topic
搜索
必要时 Research
返回 3～5 个选题
```

---

## Case 2

```text
用户：
第三个方向可以，但换几个还在招聘的
```

系统：

```text
保留方向
Research
重新组织候选
```

---

## Case 3

```text
用户：
写吧
```

系统：

```text
理解当前 confirmed topic
调用 Script
```

---

## Case 4

```text
用户：
第二段短一点
```

系统：

```text
修改当前 Draft
保存新版
```

---

## Case 5

```text
用户：
这个稿子有没有风险
```

系统：

```text
调用 Review
```

---

## Case 6

```text
用户：
生成吧
```

系统：

```text
调用数字人 Tool
```


---

## Case 7｜候选展示顺序稳定

```text
TopicResult 数据库存储顺序：A/B/C
前端实际展示：B/C/A
```

系统必须根据当前 `candidate_batch_id + display_index` 将“第三个”解析为 A，不能直接取数组 `[2]`。

---

## Case 8｜Research 无法确认仍在招聘

```text
用户：第三个现在还招吗？
```

如果官方页面打不开或截止时间无法确认：

```text
recruitment_status = unknown
```

Supervisor 明确告诉用户“目前无法确认”，不能补成肯定结论。

---

## Case 9｜改稿保存失败

```text
Script Agent 已返回修改结果
SQLite commit 失败
```

系统：

```text
current_draft 仍保持旧版本
不发送 draft_updated 成功事件
返回可重试错误
```

---

## Case 10｜并发连续修改

用户快速发送：

```text
第二段短一点
开头再换成问句
```

系统按 session 串行执行：

```text
v1 → v2 → v3
```

不得生成两个彼此覆盖的 v2。

---

## Case 11｜数字人提交超时

如果请求超时且不知道供应商是否受理：

```text
状态 = unknown_submission
先查询，不盲目重试
```

同一 `idempotency_key` 最终只能对应一个有效生成任务。

---

## Case 12｜服务重启恢复

Go / Python 任一服务重启后：

```text
Thread checkpoint 可恢复
Business Task State 可恢复
current draft version 可恢复
已有 generation provider_task_id 可继续查询
```

不能因为进程重启丢失“当前做到哪一步”。


---

# 63. Evaluation

评估：

```text
对话理解
状态连续性
Agent 自主调用
Tool 使用
事实可靠性
稿件质量
Agent 边界
生成授权
```


---

# 63.5 运行上限、超时与重试

V6 不再使用 Agents SDK 的 `max_turns`，而采用 LangGraph `recursion_limit` + 应用层调用计数。

第一版初始默认值：

```text
Supervisor recursion_limit：12
单次用户请求 Specialist 调用总数：最多 6 次
同一 Specialist 单请求调用：最多 2 次
Research ↔ Topic 往返：最多 2 轮
普通 HTTP Tool 重试：最多 1 次（仅 retryable error）
数字人 submit：禁止盲重试，必须走幂等确认
```

Tool 超时显式配置：

```text
search：20s
fetch_source：15s
digital_human_submit：30s
generation_status：10s
```

应用层在 `ContentAppContext` 或 request-scoped counter 中记录：

```text
specialist_call_count
research_round_count
tool_retry_count
```

处理原则：

```text
超时
达到 recursion_limit
达到 Specialist 调用上限
多次调研仍无结论
↓
返回当前已确认事实 + uncertainties
↓
明确资料不足
```

不要通过提高 recursion_limit 掩盖错误循环。

---

# 64. 自主性验收

必须出现：

```text
不同输入
→ 不同 Specialist 调用
```

例如：

```text
“第三个现在还招吗？”
→ Research
```

而：

```text
“第三个写一版”
→ Script
```

而：

```text
“这个稿有没有问题？”
→ Review
```

而：

```text
“今天做啥？”
→ Topic / Research
```

不能所有输入都走同一固定链路。

---

# 65. Tool 自主性验收

Topic / Research 不应：

```text
每次机械调用全部 Tool。
```

应该：

```text
根据问题选择必要 Tool。
```

---

# 66. 状态验收

必须正确理解：

```text
这个
第三个
刚才那个
第二段
这版
生成吧
```

还必须验证：

```text
“第三个”来自当前展示 batch，而不是任意数组位置
改稿后旧 Review 失效
改稿后旧 Generation Authorization 失效
切换 Topic 后旧 Draft 保留但不再是 current
同一 session 并发消息不会覆盖版本
同一生成请求重试不会重复提交
```

---


# 66.5 Long-term Memory 验收

必须验证：

```text
新会话开始后仍然可以查询历史选题
Script Agent 可以读取过去最终稿
Script Agent 可以读取用户历史修改记录
最终采用稿可以写入长期内容库
内容发布数据可以后续更新
不同 Thread / Session 之间长期内容数据仍然存在
```

同时检查：

```text
临时候选没有被错误写入长期 Memory
未验证模型推测没有被写入长期 Memory
```

---

# 67. 边界验收

Supervisor：

```text
不替专业 Agent 干全部工作
```

Topic：

```text
不编造事实
```

Research：

```text
不替用户决定最终内容
```

Script：

```text
不编造未验证招聘信息
```

Review：

```text
不擅自替用户生成数字人
```

---

# 68. Tracing 展示

作品集中展示一次真实 Trace：

```text
Supervisor
↓
topic_expert
↓
search
↓
research_expert
↓
topic_expert
↓
用户确认
↓
script_expert
↓
用户修改
↓
content_reviewer
↓
digital_human
```

---

# 69. 作品集前端展示

前台可以同时展示：

```text
聊天
当前内容
Agent 工作动画
实时 Trace
最终数字人
```

这会比只有聊天框更容易让面试官理解：

```text
这真的是 Multi-Agent，
不是一个大 Prompt。
```

---

# 70. 本期不做

```text
自动发布
自动账号运营
批量无人化生产
评论自动回复
自动归纳人格/偏好的复杂长期 Memory
向量数据库作为强依赖
多租户权限
高并发平台
复杂工作流引擎
```

---

# 71. 与投放诊断 Agent 的技术区别

两个项目现在可以处于同一个 LangChain / LangGraph 技术生态，但架构层级不同。

投放诊断 Agent：

```text
Custom LangGraph 为主
```

更适合：

```text
明确分析节点
确定性数据处理顺序
补证据循环
复杂 State
workflow + agent 混合
```

内容生产 Agent：

```text
LangChain create_agent 为主
+
LangGraph runtime 为底座
```

更适合：

```text
Supervisor
Subagents-as-tools
连续聊天
动态能力选择
Thread persistence
按需 Custom Graph
```

因此两个项目不会变成“同一种项目”。

真正的区别是：

```text
投放诊断
= Graph-first

内容生产
= Agent-first, Graph-when-needed
```

---

# 72. 两个项目共同能力

共同：

```text
业务 Principles
专业 Agent
Tool
结构化输出
Trace
人机协作
```

区别：

```text
一个展示复杂 orchestration
一个展示对话式 Agent 产品
```

---

# 73. 一句话技术叙事

> 在已有 Go 版 AI 选题、DeepSeek 口播稿生成和数字人生产能力基础上，新增 Python LangChain + LangGraph Agent Service，将原有按钮驱动工具升级为对话式 Multi-Agent 内容生产系统。Supervisor 使用 LangChain `create_agent` 持有用户 thread，并以 Subagents-as-tools 自主调用 Topic、Research、Script、Content Review 等专业 Agent；LangGraph Checkpointer 提供多轮会话持久化、恢复、interrupt 与 streaming，SQLite Business State 管理候选批次、稿件版本、审核状态和生成授权，最终通过事务、版本校验、并发串行和数字人幂等提交保证 Agent 自主性不会破坏业务状态一致性。

---

# 74. 最终定义

本项目不是：

```text
把按钮换成聊天框
```

也不是：

```text
为了叫 Multi-Agent，做 5 个 Agent 固定排队执行
```

而是：

```text
用户只通过连续对话表达目标
↓
Supervisor 动态决定调用哪个专业能力
↓
Specialist 在隔离上下文中完成专业任务
↓
LangGraph 保存 thread 与执行状态
↓
Business State 保证版本 / 授权 / 幂等
↓
长期内容库持续沉淀真实内容资产
```

用户只需要说：

```text
今天做啥
换几个
第三个
写吧
改一下
这个可以
生成吧
```

系统负责：

```text
理解
调度
调查
写作
审核
状态维护
长期内容记忆
工具调用
生成执行
```

但：

```text
Agent 自主性
≠
业务状态随模型自由修改
```

Agent 负责选择与生成；LangGraph 负责可恢复运行；业务代码负责事实一致性。

这就是 V6 LangChain + LangGraph 内容生产 Multi-Agent 的完整定义。

---

# 75. 官方技术参考

开发时优先以 LangChain / LangGraph 官方文档为准：

- LangChain Overview / `create_agent`  
  https://docs.langchain.com/oss/python/langchain/overview

- LangChain Multi-Agent  
  https://docs.langchain.com/oss/python/langchain/multi-agent

- Subagents pattern  
  https://docs.langchain.com/oss/python/langchain/multi-agent/subagents

- Short-term Memory / Checkpointer  
  https://docs.langchain.com/oss/python/langchain/short-term-memory

- LangGraph Persistence  
  https://docs.langchain.com/oss/python/langgraph/persistence

- LangGraph Memory / Store  
  https://docs.langchain.com/oss/python/langgraph/add-memory

- LangGraph Interrupts  
  https://docs.langchain.com/oss/python/langgraph/interrupts

- LangGraph Streaming  
  https://docs.langchain.com/oss/python/langgraph/streaming

- LangChain Structured Output  
  https://docs.langchain.com/oss/python/langchain/structured-output

- LangChain Human-in-the-loop  
  https://docs.langchain.com/oss/python/langchain/human-in-the-loop

- LangChain Guardrails / Middleware  
  https://docs.langchain.com/oss/python/langchain/guardrails

- DeepSeek Integration  
  https://docs.langchain.com/oss/python/integrations/chat/deepseek

- LangSmith Observability  
  https://docs.langchain.com/langsmith/observability

- LangGraph SQLite Checkpointer  
  https://github.com/langchain-ai/langgraph/tree/main/libs/checkpoint-sqlite
