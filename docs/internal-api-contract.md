# Python ↔ Go Internal API Contract v1

> Phase 0 / Task 0.6：只冻结第一版契约，不实现 endpoint。
>
> 依据：[existing-capabilities-audit.md](./existing-capabilities-audit.md)。

## 1. 范围与方向

浏览器继续只访问 Go public API。Go 作为同源 gateway，把 Agent 请求转发给 Python；Python Agent 再通过 Go capability API 使用现有搜索、写稿和数字人能力。

```text
Browser
  -> Go public API
  -> Go -> Python Agent API
  -> Python Agent -> Go capability API
```

本文件中的 endpoint 分为两类：

| 方向 | endpoint | 第一版状态 |
|---|---|---|
| Go → Python | `POST /internal/agent/chat` | 新增，Task 1.x 实现 |
| Go → Python | `GET /internal/agent/tasks/{session_id}` | 新增，Task 1.x 实现 |
| Go → Python | `GET /internal/agent/events/{session_id}` | 新增，事件流阶段实现 |
| Go → Python | `GET /health` | 新增，Python 服务启动时必须提供 |
| Python → Go | `POST /internal/tools/search-platform` | 新增内部适配器；复用现有搜索 Service |
| Python → Go | `POST /internal/tools/search-recruitment` | 新增内部适配器；复用现有搜索 Service |
| Python → Go | `POST /internal/tools/fetch-source` | 新增；当前没有独立 public endpoint |
| Python → Go | `POST /internal/tools/write-script` | 新增内部适配器；复用现有 Go 写稿能力 |
| Python → Go | `POST /internal/tools/generate-digital-human` | 新增内部适配器；复用 Chanjing Client |
| Python → Go | `GET /internal/tools/generation/{provider_task_id}` | 新增内部适配器；复用 Chanjing 状态查询 |

现有的 `/api/topic-search`、`/api/scripts/generate`、`/api/tasks` 和 `/api/tasks/{id}` 是浏览器 public API，不直接作为 Python 的长期内部契约。内部 endpoint 需要保留结构化错误、request ID 和 capability 边界。

## 2. 通用约定

### 2.1 编码、请求 ID 与字段命名

- 所有请求和响应使用 UTF-8 JSON；字段统一使用 `snake_case`。
- 每次调用都必须带 `X-Request-ID` header；POST JSON 同时带同名 `request_id` 字段。服务必须在成功和失败响应中原样返回 `request_id`。
- `request_id` 是一次 HTTP 调用的关联 ID，不等同于 `session_id`、`thread_id`、`task_id` 或 `provider_task_id`。
- 第一版 API 使用 `session_id` 作为对外会话字段；Python 内部将它一对一映射为 LangGraph `thread_id`，不另造第二个会话主键。
- 时间统一使用 UTC RFC3339 字符串；URL 使用绝对 HTTP(S) URL。
- 未列出的字段不得改变已有字段含义；新增字段必须向后兼容。

请求 header 示例：

```http
Content-Type: application/json
X-Request-ID: req_01JABC123
```

### 2.2 统一错误响应

所有内部 endpoint 的非 2xx 响应都使用以下结构：

```json
{
  "error_code": "CAPABILITY_TIMEOUT",
  "message": "数字人状态查询超时",
  "retryable": true,
  "request_id": "req_01JABC123",
  "details": {}
}
```

字段约定：

| 字段 | 类型 | 说明 |
|---|---|---|
| `error_code` | string | 稳定机器码；Python 不解析面向用户的中文文案 |
| `message` | string | 可记录和展示的简短说明，不包含密钥 |
| `retryable` | boolean | 是否允许 Agent 在同一业务语义下重试 |
| `request_id` | string | 与请求 header 一致 |
| `details` | object | 可选诊断字段；不得放原始密钥或完整 provider 凭据 |

第一版错误码集合：

```text
INVALID_REQUEST       请求字段缺失或格式错误，retryable=false
UNAUTHORIZED          内部调用身份无效，retryable=false
CAPABILITY_NOT_FOUND  资源或任务不存在，retryable=false
CAPABILITY_CONFLICT   版本、授权或状态冲突，retryable=false
CAPABILITY_REJECTED   provider 明确拒绝，retryable=false
CAPABILITY_TIMEOUT    请求或 provider 状态查询超时，按 details 判断
CAPABILITY_UNAVAILABLE Go capability 或 provider 暂时不可用，retryable=true
UPSTREAM_ERROR        provider 返回可识别错误，按 provider 状态判断
INTERNAL_ERROR        未分类服务错误，默认 retryable=false
```

HTTP 状态建议固定为：`400` 请求错误、`401`/`403` 身份错误、`404` 不存在、`409` 业务冲突、`422` provider 拒绝、`429` 限流、`502` 上游错误、`503` 暂时不可用、`504` 超时。

## 3. Go → Python Agent API

### 3.1 `POST /internal/agent/chat`

**用途：** Go public `/api/agent/chat` 的内部转发入口。Python 负责 Supervisor thread、Agent 调度和业务状态指针；Go 只负责 gateway 和同源访问。

请求：

```json
{
  "request_id": "req_01JABC123",
  "session_id": "sess_01JXYZ789",
  "message": "第三个写一版，开头换成问句",
  "client_message_id": "msg_0007",
  "metadata": {
    "source": "browser",
    "locale": "zh-CN"
  }
}
```

成功响应（同步完成或本轮已产生可展示结果）：

```json
{
  "request_id": "req_01JABC123",
  "session_id": "sess_01JXYZ789",
  "assistant_message_id": "amsg_0008",
  "status": "completed",
  "message": "已按第三个选题生成一版口播稿。",
  "state": {
    "current_topic_id": "topic_0003",
    "current_draft_id": "draft_001",
    "current_draft_version": 2
  }
}
```

`status` 第一版允许 `completed`、`running`、`interrupted`、`failed`。长耗时任务可先返回 `running`，再由 tasks/events endpoint 查询；不得把“已提交数字人”误报为“已完成”。

### 3.2 `GET /internal/agent/tasks/{session_id}`

**用途：** 刷新页面或恢复 thread 时读取 Agent 当前任务指针和业务状态摘要。完整 Draft、Review 和 Generation Record 属于 Python Business DB，不通过此接口返回全部历史。

请求：

```http
GET /internal/agent/tasks/sess_01JXYZ789
X-Request-ID: req_01JABC124
```

成功响应：

```json
{
  "request_id": "req_01JABC124",
  "session_id": "sess_01JXYZ789",
  "status": "idle",
  "current_topic_id": "topic_0003",
  "current_draft_id": "draft_001",
  "current_draft_version": 2,
  "active_review_id": null,
  "active_generation_id": null,
  "updated_at": "2026-09-18T08:00:00Z"
}
```

### 3.3 `GET /internal/agent/events/{session_id}`

**用途：** Go gateway 获取 Agent 工作状态事件。第一版可以先返回最近事件列表；后续再增加 SSE，不改变事件字段。

请求：

```http
GET /internal/agent/events/sess_01JXYZ789?after_event_id=evt_0004
X-Request-ID: req_01JABC125
```

成功响应：

```json
{
  "request_id": "req_01JABC125",
  "session_id": "sess_01JXYZ789",
  "events": [
    {
      "event_id": "evt_0005",
      "type": "agent_finished",
      "agent": "script",
      "status": "done",
      "message": "稿件版本已生成",
      "created_at": "2026-09-18T08:00:00Z"
    }
  ],
  "next_after_event_id": "evt_0005"
}
```

### 3.4 `GET /health`

**用途：** Python Agent Service liveness/readiness 最小检查。该 endpoint 不读取用户密钥，也不触发 Agent 或 Go capability。

请求：

```http
GET /health
X-Request-ID: req_health_001
```

成功响应：

```json
{
  "status": "ok",
  "service": "agent_service",
  "version": "v1",
  "request_id": "req_health_001"
}
```

## 4. Python Agent → Go Capability API

### 4.1 `POST /internal/tools/search-platform`

**状态：** 新增内部适配器。现有可复用实现为 `topicsearch.Service.SearchWithConfigFromStore`；当前 public `/api/topic-search` 不是此内部契约。

请求：

```json
{
  "request_id": "req_search_001",
  "query": "香港留学生秋招金融岗位",
  "max_results": 10
}
```

成功响应：

```json
{
  "request_id": "req_search_001",
  "query": "香港留学生秋招金融岗位",
  "total_found": 2,
  "results": [
    {
      "id": "result_001",
      "type": "campus_recruitment",
      "title": "香港金融机构校园招聘",
      "organization": "示例机构",
      "region": ["香港"],
      "time": "2026 秋招",
      "location": "香港",
      "target_audience": ["留学生"],
      "companies": ["示例机构"],
      "positions": ["金融科技"],
      "position_count": "若干",
      "education_requirement": "本科及以上",
      "school_requirement": "",
      "application_period": "2026-08-01/2026-10-01",
      "deadline": "2026-10-01",
      "benefits": "",
      "key_info": ["以原文为准"],
      "source_title": "示例来源",
      "source_url": "https://example.com/recruitment",
      "source_type": "official",
      "published_at": "2026-09-18"
    }
  ],
  "ai_processed": true,
  "search_provider": "local",
  "search_executed": true,
  "web_search_call_count": 1,
  "raw_result_count": 5,
  "accepted_result_count": 2,
  "filtered_result_count": 3
}
```

### 4.2 `POST /internal/tools/search-recruitment`

**状态：** 新增内部适配器；返回结构与 `search-platform` 相同，语义上要求招聘事实筛选。Go 仍复用同一个 `topicsearch.Response` 和配置来源，不在 Python 重写 provider、抽取或去重规则。

请求：

```json
{
  "request_id": "req_search_002",
  "query": "2026 香港银行留学生招聘",
  "max_results": 10
}
```

成功响应：

```json
{
  "request_id": "req_search_002",
  "query": "2026 香港银行留学生招聘",
  "total_found": 0,
  "results": [],
  "ai_processed": true,
  "search_provider": "local",
  "search_executed": true,
  "web_search_call_count": 1,
  "raw_result_count": 3,
  "accepted_result_count": 0,
  "filtered_result_count": 3
}
```

示例中的 `results` 允许为空；真实业务不得用示例机构或示例 URL 冒充事实。

### 4.3 `POST /internal/tools/fetch-source`

**状态：** 下一阶段新增。当前抓取逻辑是搜索 Service 内部实现，没有独立 public endpoint。

请求：

```json
{
  "request_id": "req_fetch_001",
  "source_url": "https://example.com/recruitment",
  "max_chars": 6000
}
```

成功响应：

```json
{
  "request_id": "req_fetch_001",
  "source_url": "https://example.com/recruitment",
  "final_url": "https://example.com/recruitment",
  "title": "示例来源",
  "content": "经清洗的正文内容",
  "published_at": "2026-09-18",
  "fetched_at": "2026-09-18T08:00:00Z"
}
```

### 4.4 `POST /internal/tools/write-script`

**状态：** 新增内部适配器。当前旧写稿入口是 `POST /api/scripts/generate`，实际实现为 `skill.BuildPrompt` + `llm.GenerateScript`；第一版优先复用这条 Go 链路。

请求：

```json
{
  "request_id": "req_script_001",
  "topic": "香港留学生秋招金融岗位",
  "research_result": {
    "topic_id": "topic_0003",
    "facts": [
      {
        "claim": "示例机构开放秋招",
        "source_url": "https://example.com/recruitment"
      }
    ]
  },
  "instruction": "生成两版中文口播稿，开头使用问句"
}
```

成功响应：

```json
{
  "request_id": "req_script_001",
  "script": "生成后的口播稿文本",
  "model": "configured-model",
  "skill_files": [
    "SKILL.md",
    "docs/structure-templates.md",
    "library/实操攻略.md"
  ]
}
```

`script` 是纯文本；内部适配器不得把面向浏览器的中文错误字符串当作成功内容。旧入口当前不接受 `research_result`、历史稿件或版本字段，因此在适配器真正实现前，Python 必须把需要写入的事实整理到 `topic` 或由后续 Prompt 扩展 Task 明确定义。

### 4.5 `POST /internal/tools/generate-digital-human`

**状态：** 新增内部适配器。现有可复用能力是 `chanjing.Client.CreateVideo`，现有 public 入口为 `POST /api/tasks`。本契约提前冻结 Agent 需要的授权和幂等字段；当前 Go public API 尚未实现这些校验。

请求：

```json
{
  "request_id": "req_generation_001",
  "task_id": "content_task_001",
  "draft_id": "draft_001",
  "draft_version": 3,
  "authorization_id": "auth_001",
  "idempotency_key": "idem_001",
  "script": "已获授权的稿件正文",
  "params": {
    "avatar_id": "C-avatar-001",
    "audio_man_id": "C-voice-001",
    "speed": 1.0,
    "volume": 100,
    "bg_color": "#ffffff",
    "resolution": "1080P",
    "subtitle_enabled": true,
    "figure_type": "stand_body",
    "drive_mode": "random",
    "backway": 1,
    "language": "cn",
    "compliance_watermark": false
  }
}
```

成功响应：

```json
{
  "request_id": "req_generation_001",
  "generation_id": "generation_001",
  "provider_task_id": "provider-task-001",
  "status": "pending",
  "created_at": "2026-09-18T08:00:00Z"
}
```

Go adapter 必须把 `provider_task_id` 映射到 Agent 的 `generation_id`；重复的 `idempotency_key` 必须返回原有 generation record，不得再次调用 provider。这个行为属于后续实现约束，本 Task 不声称当前 `/api/tasks` 已具备它。

### 4.6 `GET /internal/tools/generation/{provider_task_id}`

**状态：** 新增内部适配器。当前可复用来源为 `GET /api/tasks/{id}/refresh`、`scheduler.Refresh` 和 `chanjing.Client.GetVideoStatus`。

请求：

```http
GET /internal/tools/generation/provider-task-001
X-Request-ID: req_generation_status_001
```

成功响应：

```json
{
  "request_id": "req_generation_status_001",
  "generation_id": "generation_001",
  "provider_task_id": "provider-task-001",
  "status": "done",
  "progress": 100,
  "video_url": "https://res.example.com/video.mp4",
  "error_code": null,
  "error_message": null,
  "retryable": false,
  "completed_at": "2026-09-18T08:05:00Z"
}
```

第一版 `status` 允许 `pending`、`generating`、`done`、`failed`、`timeout`、`unknown_submission`。`video_url` 只有 `done` 时可以非空；`unknown_submission` 用于提交超时且无法判断 provider 是否受理的情况，不能盲目重复提交。

## 5. 现有 endpoint 映射与未实现项

| 内部契约 | 当前代码事实 | 映射 / 缺口 |
|---|---|---|
| `/internal/tools/search-platform` | `POST /api/topic-search` → `topicsearch.Service.SearchWithConfigFromStore` | 复用 Service，新增内部结构化 wrapper |
| `/internal/tools/search-recruitment` | 没有独立 recruitment endpoint | 新增 wrapper，不能在 Python 复制搜索过滤逻辑 |
| `/internal/tools/fetch-source` | 抓取逻辑藏在 `topicsearch` 内部 | 新增 capability endpoint |
| `/internal/tools/write-script` | `POST /api/scripts/generate` → `skill.BuildPrompt` → `llm.GenerateScript` | 按 Task 0.3 的 B 方案新增内部 wrapper |
| `/internal/tools/generate-digital-human` | `POST /api/tasks` → Chanjing Client | 新增 wrapper，并补 authorization/idempotency 边界 |
| `/internal/tools/generation/{provider_task_id}` | `GET /api/tasks/{id}`、`POST /api/tasks/{id}/refresh`、scheduler | 新增统一 status wrapper |
| `/internal/agent/chat` | 当前不存在 Agent Service | Phase 1 新增 Python 服务和 Go proxy |
| `/health` | 当前 Go 服务没有统一 health route | Python 服务启动时新增；Go proxy health 留给后续部署 Task |

## 6. 第一版冻结决策

1. Go JSON 文件仍由 Go capability 持有；Python 不直接读 `data/config.json`，不直接写 `tasks.json` 或 `topics.json`。
2. 搜索和写稿优先复用现有 Go Service；Python 只消费结构化 wrapper，不复制 provider、Skill 或过滤逻辑。
3. 数字人调用必须以 `generation_id`、`provider_task_id`、`authorization_id` 和 `idempotency_key` 建立映射；这些字段是契约要求，当前实现缺口留给后续 Task。
4. 所有内部调用必须保留 `request_id` 和统一错误结构；不能把面向浏览器的错误文案作为 Agent 协议。
5. `agent_service` 的 Business SQLite 与 LangGraph Checkpointer SQLite 独立于现有 Go JSON 文件；本 Task 不创建数据库。

本文件冻结后，Phase 1 可以据此搭建 Python 服务骨架、Go proxy 和 capability wrapper，不需要重新猜测现有字段或把 public endpoint 当成内部契约。
