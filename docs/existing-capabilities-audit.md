# Existing Capabilities Audit

> Task 0.1｜仓库地图与启动链路审计
>
> 审计日期：2026-09-18
>
> 本文只记录仓库地图、启动链路、路由注册、前端资源和配置来源。搜索实现细节、DeepSeek Prompt、数字人供应商 API 和 Agent 设计留给后续 Task。

## 仓库地图

### 启动入口

程序入口是根目录的 `main.go`，入口函数为 `main()`。

启动时依次完成：

1. 解析两个调试参数：`-print-creds` 和 `-test-chanjing`。
2. 通过 `os.Executable()` 找到当前可执行文件路径，并把可执行文件所在目录作为运行根目录。
3. 在运行根目录创建 `data/`，初始化 `config.json`、`tasks.json`、`topics.json`、`avatars.json` 和 `voices.json`（文件不存在时才创建）。
4. 从嵌入的 `chanjing/person id/` 图片目录初始化本地形象列表，并初始化默认人声列表。
5. 读取 `data/config.json`，创建 Chanjing 客户端。
6. 创建任务轮询调度器，读取 `data/tasks.json` 并恢复未完成任务。
7. 创建 `handlers.Handler`，注册 HTTP API。
8. 从嵌入文件系统挂载前端资源和数字人预览图片。
9. 在 `:8899` 启动 HTTP 服务，并尝试打开 `http://localhost:8899`。

主启动链路可以表示为：

```text
main.go:main()
  → executablePath()
  → data/ 初始化
  → seedAvatars / seedVoices
  → store.LoadConfig
  → scheduler.New + Restore
  → handlers.New + Register
  → embed.FS 挂载 frontend 和人物图片
  → http.Server :8899
```

### 启动命令

生产/演示方式是直接运行已编译的 Windows 程序：

```powershell
.\koubo-video-tool-2.0.exe
```

本地编译方式由 `build.bat` 定义：

```bat
build.bat
```

脚本设置 `GOOS=windows`、`GOARCH=amd64`、`CGO_ENABLED=0`，执行：

```bat
go build -ldflags="-s -w" -o koubo-video-tool.exe .
```

开发时也可以直接使用 Go 的标准运行命令：

```powershell
go run .
```

当前程序没有端口命令行参数；服务端口在 `main.go` 中固定为 `8899`。

`main.go` 还提供两个调试入口：

```text
-print-creds     读取并打印解密后的配置凭据后退出
-test-chanjing   使用固定测试参数直接调用 Chanjing 后退出
```

### HTTP 路由注册位置

路由集中注册在 `handlers/handlers.go` 的 `(*Handler).Register()`。当前 API 为：

| 路由 | 方法 | 作用 |
|---|---|---|
| `/api/avatars` | GET | 读取并同步数字人形象列表 |
| `/api/voices` | GET | 读取人声列表 |
| `/api/scripts/generate` | POST | 根据主题生成口播初稿 |
| `/api/topic-search` | POST | 执行 AI 选题搜索 |
| `/api/topics` | GET / POST / DELETE | 读取、保存、删除选题库记录 |
| `/api/tasks` | GET / POST / DELETE | 读取、创建、清空视频任务记录 |
| `/api/tasks/{task_id}` | GET / DELETE | 查询或删除单个任务记录 |
| `/api/tasks/{task_id}/retry` | POST | 重试视频生成任务 |
| `/api/tasks/{task_id}/refresh` | POST | 主动刷新供应商任务状态 |
| `/api/settings` | GET / PUT | 读取或保存应用设置 |

路由注册使用 Go 标准库 `net/http` 默认 mux，不使用独立 Web 框架。API handler 负责读取配置、调用现有模块并返回 JSON。

### 前端资源位置

前端静态资源位于根目录 `frontend/`，主要文件包括：

```text
frontend/index.html      首屏/页面容器
frontend/app.js          页面加载与公共 API 调用逻辑
frontend/main.html       主工作台页面
frontend/setup.html      首次设置页面
frontend/tutorial.html   使用教程内容
frontend/style.css       页面样式
```

`main.go` 使用 `//go:embed frontend/* ...` 将前端资源编译进程序，运行时通过：

```text
/                       → embedded frontend 文件系统
/img/person/            → embedded chanjing/person id 图片文件系统
```

因此运行时不要求旁边存在源码级 `frontend/` 目录；源码目录仍是重新编译时的输入。

### 主要业务目录表

| 目录/文件 | 当前作用 |
|---|---|
| `main.go` | 程序启动、运行目录初始化、服务启动和静态资源挂载 |
| `handlers/` | HTTP API handler、请求校验、模块调用和 JSON 响应 |
| `store/` | JSON 配置、视频任务、形象、人声、选题的本地读写；配置密钥加解密入口 |
| `scheduler/` | Chanjing 视频任务轮询、状态恢复和状态回调 |
| `chanjing/` | Chanjing 客户端、视频创建/查询及资源相关接口 |
| `topicsearch/` | AI 选题搜索服务及搜索结果结构；本 Task 不展开内部搜索流程 |
| `llm/` | OpenAI 兼容模型请求、稿件生成及工具调用基础结构；本 Task 不展开 Prompt |
| `skill/` | 读取本地或 GitHub Skill，并构造口播稿生成所需提示词 |
| `skills/` | 仓库内置的 Skill 副本和知识素材 |
| `crypto/` | 本地配置敏感字段的加解密实现 |
| `frontend/` | 浏览器端页面、交互和样式 |
| `experiments/` | 搜索、接口等独立探针，不是主启动链路 |
| `data/` | 运行时生成的配置和 JSON 数据，已被 `.gitignore` 忽略 |

### 编译模块

`go.mod` 声明的模块名为 `koubo-video-tool`，Go 版本为 `1.22.10`。外部依赖目前只有 `github.com/google/uuid`（间接依赖标记），其余能力主要使用 Go 标准库和仓库内模块。

## 配置来源

### 配置文件位置

正式运行时配置文件是：

```text
<可执行文件所在目录>/data/config.json
```

路径由 `main.go` 的 `dataDir := filepath.Join(exeDir, "data")` 决定，不是当前工作目录下固定的 `data/`。

首次运行时如果文件不存在，程序写入空对象 `{}`，之后由前端设置页通过 `PUT /api/settings` 保存配置。

### 配置结构

配置结构定义在 `store/store.go` 的 `store.Config`，当前分为：

```text
chanjing.app_id
chanjing.secret_key

llm.api_url
llm.api_key
llm.model

search.provider
search.api_key

skill.source
skill.local_path
skill.github_url
skill.github_cache_until
```

### 密钥读取与保存

`store.LoadConfig()` 从 JSON 文件读取配置，并通过 `crypto.Decrypt()` 解密 Chanjing、LLM 和搜索 API Key。带 `ENC:` 前缀的值表示已加密；兼容旧的明文值，保存时由 `store.SaveConfig()` 再加密。

`GET /api/settings` 返回配置时会将已配置的 SecretKey 和 API Key 替换为 `store.MaskedValue`，避免把真实密钥发送到前端。设置页提交掩码值时，handler 会保留磁盘中的原密钥。

当前仓库不读取 `.env` 作为运行配置，也没有发现独立的 Python Agent 配置入口；运行配置的事实来源是上述 `data/config.json`。

## Search Capability

### 对外入口与真实调用链

当前浏览器使用的搜索入口是：

```text
POST /api/topic-search
请求体：{"query":"香港外企"}
```

入口注册在 `handlers/handlers.go` 的 `(*Handler).Register()`，实际 handler 是 `handleTopicSearch`。完整调用链如下：

```text
HTTP POST /api/topic-search
  → handlers.handleTopicSearch
  → store.LoadConfig(data/config.json)
  → context.WithTimeout(..., 300s)
  → Handler.TopicSearchWithConfig
  → topicsearch.Service.SearchWithConfigFromStore
  → Service.SearchWithConfig
  → provider 分支
  → 外部搜索 / LLM 整理
  → topicsearch.Response
  → JSON 响应
```

`handleTopicSearch` 的输入只有 `query`。它会拒绝空 query；配置读取失败返回 500；未配置 LLM API 地址或密钥返回 400；搜索链路错误统一记录日志并返回 502，同时通过 `topicSearchErrorMessage` 转成面向用户的错误信息。

### `topicsearch.Service` 的入口

`topicsearch/search.go` 提供两个应用级入口：

```go
func (s *Service) Search(ctx, query, apiURL, apiKey, model)
func (s *Service) SearchWithConfigFromStore(ctx, query, cfg store.Config)
```

`SearchWithConfigFromStore` 只是把 `store.Config` 转成 `SearchConfig`，然后调用：

```go
SearchWithConfig(ctx, query, cfg.LLM.APIURL, cfg.LLM.APIKey, cfg.LLM.Model, SearchConfig{
    Provider: cfg.Search.Provider,
    APIKey:   cfg.Search.APIKey,
})
```

`SearchWithConfig` 的 provider 分支为：

| 配置 | 实际路径 |
|---|---|
| 空、`local`、`html` + LLM 地址为 `api.deepseek.com` | DeepSeek 原生 Anthropic-compatible `web_search` |
| 空、`local`、`html` + 其他兼容 LLM 地址 | Go 本地 `web_search` Tool loop |
| `tavily` | Tavily 结构化搜索 + LLM 规划/抽取 |
| `brave` | Brave 结构化搜索 + LLM 规划/抽取 |
| 其他值 | 返回“不支持的搜索服务”错误 |

### 路径 A：DeepSeek 原生搜索

当 `llm.api_url` 的 hostname 是 `api.deepseek.com` 且 provider 为空、`local` 或 `html` 时，调用：

```text
topicsearch.Service.searchWithDeepSeekNative
  → topicsearch.Service.searchWithBuiltInSearch
  → llm.DeepSeekSearchJSONContext
```

`llm.DeepSeekSearchJSONContext` 将 API 地址规范化为：

```text
<api_url>/anthropic/v1/messages
```

请求使用：

```text
x-api-key: <llm.api_key>
anthropic-version: 2023-06-01
tool: web_search_20250305
max_uses: 5
```

它解析 `server_tool_use`、`web_search_tool_result` 和最终 `text` 块，返回：

```go
llm.ResponsesJSONResult{
    Content,
    WebSearchCallCount,
    WebSearchURLs,
}
```

最多进行 3 轮请求，用于处理 `stop_reason == "pause_turn"` 的服务端搜索续接；只有 `end_turn` 才视为正常结束。没有执行 `web_search`、搜索工具报错、响应不是 2xx、响应 JSON 无法解析、没有最终文本或连续暂停 3 轮，都会返回错误。

`searchWithBuiltInSearch` 会对模型 JSON 做结构化解析和招聘相关性过滤。如果首次过滤后少于 5 条，且外层 context 尚未超时，会再调用一次相同搜索函数补充结果。随后按 `topicsearch.Result` 输出，并限制最多 15 条。

### 路径 B：Go 本地搜索 Tool loop

当不是 DeepSeek 原生地址时，`local` / `html` provider 走：

```text
topicsearch.Service.searchWithLocalTool
  → llm.ChatWithToolsContext
  → 模型请求 web_search Tool
  → executeLocalSearchTool
  → searchCandidates
  → 本机搜索引擎 HTML
  → Tool message 回传模型
  → 最终 JSON 整理
```

这里模型只负责决定查询词和整理结果，网络搜索由 Go 进程执行。Tool 定义名为 `web_search`，参数只有非空 `query`。

单次本地 Tool loop 最多 3 轮模型调用；超过轮数后会使用已经收集的证据再做一次最终整理，而不是继续循环。候选证据内部结构为：

```text
candidate.title
candidate.url
candidate.snippet
candidate.content
candidate.published_at
```

本地搜索默认 `SearchURL` 是：

```text
https://www.baidu.com/s
```

在 Windows 上，百度、Bing、Brave、DuckDuckGo 等系统搜索地址优先通过 `powershellGet` 调用 `powershell.exe Invoke-WebRequest`；其他地址使用 Go `http.Client`。单条系统搜索请求的 PowerShell 超时为 15 秒。如果默认搜索失败、返回空结果或页面不可解析，并且地址属于系统搜索，`searchOneWithFallback` 会尝试 Bing：

```text
https://www.bing.com/search
```

这属于已有的 best-effort fallback，不是重试循环。

### 路径 C：Tavily / Brave 结构化搜索

`searchWithConfig` 的流程为：

```text
LLM planning（最多 12s）
  → 生成 QueryIntent 与扩展查询词
  → searchCandidates 并发搜索
  → 候选 URL 去重与招聘候选优先级排序
  → 最多抓取 6 个页面
  → LLM extraction（最多 70s）
  → filterAndDeduplicate
  → topicsearch.Response
```

规划失败会记录日志并降级到默认意图，搜索和最终抽取仍会继续；最终 LLM 抽取失败则整个请求失败，不会把网页标题直接当成 AI 结果。

Tavily：

```text
POST https://api.tavily.com/search
```

API Key 放在 JSON body 的 `api_key` 字段，默认最多返回 5 条，取 `title`、`url`、`content` / `raw_content` 和 `published_date`。

Brave：

```text
GET https://api.search.brave.com/res/v1/web/search?q=...&count=5
X-Subscription-Token: <search.api_key>
```

两种结构化 provider 都只返回候选事实证据，后续仍由 LLM extraction 和 `filterAndDeduplicate` 做类型、相关性、来源、去重及报名窗口过滤。

### 网页抓取与超时

通用 `topicsearch.Service` 的 HTTP client：

```text
总超时：18s
TLS handshake：10s
Response header：15s
最大空闲连接：8
```

候选页面抓取使用 `context.WithTimeout(..., 8s)`，请求最多读取 160 KiB，清洗后的正文最多保留 6000 字符。搜索阶段最多保留 32 个候选，最多并发 4 个查询；抓取阶段最多并发处理前 6 个候选。

外层 `/api/topic-search` 还会给整个请求设置 300 秒 context deadline。普通 local/Tavily/Brave 查询没有通用自动重试；已有的恢复行为只有：

```text
本地 HTML 搜索失败/无结果 → Bing fallback
DeepSeek 原生搜索 pause_turn → 最多 3 轮续接
DeepSeek 过滤结果少于 5 条 → 额外补搜 1 次
```

每条并发查询失败会记录 `search_end` trace 和日志并丢弃该批次；如果最终没有任何成功候选，则返回“搜索服务没有返回可用结果”。

### 返回值与业务判断边界

公共返回类型是 `topicsearch.Response`：

```text
query
total_found
results[]
ai_processed
search_provider
search_executed
web_search_call_count
raw_result_count
accepted_result_count
filtered_result_count
```

`results[]` 的 `topicsearch.Result` 包含标题、类型、组织、地区、招聘对象、公司、岗位、人数、学历、申请窗口、截止时间、来源标题、来源 URL、来源类型和发布时间等结构化字段。

因此当前搜索能力不是“只返回原始网页”：

```text
candidate / provider response
= 原始候选事实证据

LLM extraction + filterAndDeduplicate
= 已经混入招聘类型、相关性、来源优先级、去重和当前报名窗口判断
```

它还没有替用户决定最终内容选题，也没有生成口播稿；但它已经包含一层招聘事实有效性筛选。Python Agent 将来应把它视为“搜索与事实整理能力”，不要重复实现第二套搜索过滤逻辑。

### Python 复用结论

当前已经存在可用的公共 HTTP API：

```text
POST /api/topic-search
```

但当前没有独立的 `/internal/tools/search-platform` 或 `/internal/tools/search-recruitment` API。该公共 endpoint 以浏览器用户错误消息和 `data/config.json` 为中心，尚未定义 Python Agent 所需的内部鉴权、请求 ID、结构化错误契约或 capability-specific 路由。

结论：

```text
Python 不应复制 topicsearch/search.go。
Python 也不应直接依赖浏览器语义的错误文本。
后续需要新增一个薄的 Go internal search adapter，
内部调用现有 Service.SearchWithConfigFromStore，
返回结构化 Response / error code。
```

本 Task 不新增 adapter，也不修改搜索代码。当前已确认的可复用核心函数是：

```text
topicsearch.Service.SearchWithConfigFromStore
topicsearch.Service.SearchWithConfig
```

### 已停用或不稳定的搜索链路

以下路径已在代码和注释中明确为非生产主路径：

1. `searchWithDeepSeekResponses` 仅保留为 Responses API 兼容性探针；当前生产 DeepSeek 搜索走 Anthropic-compatible native path。
2. `experiments/deepseek-responses-web-search/test_web_search.py` 是独立能力探针，不是应用主搜索路径。
3. 依赖公共搜索引擎 HTML 的本地 provider 会受到网页结构变化、访问限制和 Windows 网络栈差异影响；代码提供 Bing fallback，但没有保证所有搜索页面长期稳定。

## 当前未确认的问题

以下问题属于后续审计或实现 Task，Task 0.1 不展开：

1. 搜索服务的具体 provider 分支、搜索工具调用、超时和重试边界。
2. DeepSeek/兼容模型的完整请求 Prompt、结构化输出和失败处理细节。
3. Chanjing 资源同步的完整供应商 API 契约；Task 0.4 已确认视频创建、状态查询和结果 URL，资源/形象同步的完整分页策略留待后续审计。
4. 现有 Go 服务是否要作为 Python Agent Service 的 gateway，或是否已有可复用的内部 HTTP 接口。
5. Python Agent Service、LangChain、LangGraph Checkpointer 和 Business State 的启动方式与目录尚未存在于当前仓库。
6. 当前服务固定使用默认 mux 和 `:8899`，后续如果增加代理或多服务部署，需要另行确定端口和路由边界。
7. `go run .` 是 Go 标准开发启动方式，但项目当前正式说明主要面向 `build.bat` 和已编译 Windows 程序，尚未定义开发环境依赖清单。

## 审计边界结论

本 Task 已定位启动入口、启动命令、路由注册位置、前端资源、一级业务模块和配置来源；没有修改业务代码，也没有新增 Python 或 Agent 实现。

## 口播稿与 DeepSeek 能力审计（Task 0.3）

本节只记录当前仓库已经存在的写稿能力，未修改写稿链路、Prompt、模型配置或接口契约。

### 真实入口与调用链

浏览器通过 `POST /api/scripts/generate` 提交：

```json
{"topic":"用户提供的选题文本"}
```

实际链路位于 `handlers/handlers.go` 的 `handleGenerateScript`：

```text
POST /api/scripts/generate
  -> json.Decoder 读取 topic
  -> store.LoadConfig(data/config.json)
  -> skill.BuildPrompt(&cfg, topic)
  -> llm.GenerateScript(cfg.LLM.APIURL, cfg.LLM.APIKey, cfg.LLM.Model, topic, prompt)
  -> llm.chatContext(...)
  -> POST <cfg.LLM.APIURL>/v1/chat/completions
  -> choices[0].message.content
  -> {"script":"..."}
```

`Register` 在启动时把 `/api/scripts/generate` 绑定到该 handler。入口只接受 `POST`，空 `topic` 返回 400；配置加载、Skill 读取和模型调用失败分别返回 500，未配置 LLM 地址或密钥时返回 400。当前没有单独的 DeepSeek SDK client 或 Script Agent 对象；`llm` 包提供的是通用 OpenAI-compatible HTTP 传输，实际供应商和模型由 `LLM.APIURL`、`LLM.APIKey`、`LLM.Model` 决定。

### DeepSeek 客户端、鉴权与请求参数

`store.LoadConfig` 从 `data/config.json` 加载配置，并解密保存的 LLM 密钥。写稿请求使用 `llm.chatContext`：

```text
POST strings.TrimRight(apiURL, "/") + "/v1/chat/completions"
Authorization: Bearer <api_key>
Content-Type: application/json
超时：180 秒
响应读取上限：2 MiB
```

请求体是 OpenAI Chat Completions 的最小结构：

```json
{
  "model": "<cfg.llm.model>",
  "messages": [
    {"role":"system", "content":"<skill.BuildPrompt 返回值>"},
    {"role":"user", "content":"请根据以下话题生成口播稿：\n\n<topic>"}
  ]
}
```

当前写稿调用没有流式响应、重试、工具调用或结构化输出参数。响应先解析为 `chatResponse`，只取 `choices[0].message.content`；HTTP 非 2xx、API 返回 `error`、空 `choices`、传输失败和 JSON 解析失败都会转成错误，handler 再返回 `生成失败: ...`。因此写稿结果的应用层类型是一个普通字符串，不包含模型、Skill 文件、版本或证据字段。

### Prompt / Skill 的真实来源

`skill.BuildPrompt` 是写稿 Prompt 的唯一组装入口，支持 `cfg.Skill.Source` 为 `local` 或 `github`：

```text
local（默认候选根目录）
  skills/Koubo-rewrite-skill-local
  skills/Koubo-rewrite-skill

必需文件
  SKILL.md
  docs/structure-templates.md
  docs/jargon-lexicon.md
  docs/emotional-protocol.md
  docs/domain-knowledge.md

按 topic 选取的 library 文件
  招聘会活动.md / 单企业直招.md / 平台工具.md
  认知打破.md / 赛道红利.md / 实操攻略.md

可选示例
  examples/input-sample.md
  examples/output-sample.md
```

选题关键词决定 `library/*.md` 的选择，未命中时使用 `实操攻略.md`。所有读取内容最后会追加 `runtimeGuardrails`，约束输出为中文短视频口播稿、给出 2-3 个独立版本、禁止 Markdown/JSON/YAML/XML/工具调用或命令，并要求只输出可直接复制的脚本。`local` 源读取失败时存在兼容旧 Markdown 树的回退；`github` 源会把仓库缓存到临时目录，缓存有效期为 24 小时，GitHub API 请求超时为 20 秒，然后仍由同一个 `buildPromptFromDir` 组装 Prompt。handler 在 GitHub 源成功加载后会保存更新时间。

因此当前实际传给模型的 system message 是 Skill 文件拼接结果与运行时约束，不是硬编码在 handler 中的一段短 Prompt；user message 只包含固定前缀和本次 `topic`，没有自动附加历史对话、上一次稿件或搜索证据。

### 已验证的业务能力

现有链路可以确认具备以下能力：

1. 通过浏览器输入一个选题，调用配置的 OpenAI-compatible 模型生成口播稿。
2. 按本地 Skill 目录或 GitHub Skill 源装载写稿规范、领域知识、术语、情绪协议、结构模板和示例。
3. 根据选题关键词选择不同业务资料库文件，并追加统一运行时约束。
4. 前端把返回字符串展示到 `#scriptContent`，允许用户在浏览器中进入 `contenteditable` 状态进行手工编辑、保存或放弃。
5. 保存后的文本写入浏览器 `localStorage` 偏好；点击生成视频时，前端从保存文本读取并作为 `POST /api/tasks` 的 `script` 字段提交。视频任务存储的是提交时的脚本文本，不是写稿过程的版本历史。

### 局部改写、上下文与历史边界

当前没有“改写选中段落”“基于上一版继续生成”“生成多个版本并比较”或“保存草稿版本”的后端接口。后端公开的写稿入口只有按 `topic` 全量生成的 `/api/scripts/generate`；前端编辑仅发生在当前页面和浏览器本地偏好中，未回写服务端稿件库。`llm.GenerateScript` 每次只发送一个 system message 和一个带 topic 的 user message，不携带会话 ID、历史消息、任务 ID、上一稿、人工修改记录或搜索结果引用。

### 当前限制与稳定性事实

- 代码层面没有 DeepSeek 专用客户端；是否使用 DeepSeek 取决于配置的兼容 API 地址和模型名，不能从写稿函数本身推断具体供应商。
- 写稿输出是未校验的纯文本，没有 JSON Schema、字段级解析、版本号、来源证据或内容审核结果。
- 没有流式输出，单次普通请求最多等待 180 秒；写稿调用没有专用重试或幂等键。
- Skill GitHub 源依赖远端 GitHub API 和 24 小时临时缓存；缓存失效或远端不可用会阻断 Prompt 构建，之后由 handler 返回 Skill 源读取失败。
- 选题检索链路的结构化结果不会自动进入写稿 user message；若上游只把搜索结果展示给用户，写稿接口仍只接收用户传入的 topic 字符串。
- 前端本地保存可恢复最近一次文本，但服务端没有用户、会话、草稿、审批或并发编辑模型。

### 对 Script Agent 的复用建议

审计结论选择 **B：优先复用现有 Go 写稿能力**。建议未来 Script Agent 通过一个薄的内部适配层调用现有 Skill 组装和 `llm.GenerateScript`，理由是：当前链路已经集中处理了加密配置、OpenAI-compatible 传输、Skill 本地/GitHub 来源、业务资料库选择、运行时约束和前端既有行为；直接在 Python/LangChain 中重新实现 DeepSeek 调用与 Prompt 装配会产生两套供应商配置、Skill 规则和错误处理。

这个结论只针对复用方向，不代表本 Task 新增 adapter。现有浏览器 endpoint 返回纯字符串并使用面向用户的错误文本，未来若作为 Agent 内部能力复用，应另行定义内部结构化契约（至少包括脚本文本、模型、Skill 文件列表和错误码），再处理超时、版本、审批与历史上下文。模式 A（由新 Agent 直接调用 DeepSeek）只有在明确模型供应商归属、Prompt 独立维护、流式/结构化输出和会话状态方案后才适合采用；本 Task 不执行该迁移。

## 数字人能力审计（Task 0.4）

本节只记录现有 Chanjing 数字人提交、任务查询和轮询能力，未实现授权、幂等或新的 API 适配器。

### Provider / Adapter 与真实调用链

当前 provider adapter 是 `chanjing/chanjing.go` 的 `chanjing.Client`，通过 `ClientInterface` 注入 handler 和 scheduler。生产启动在 `main.go` 中读取 `data/config.json` 的 Chanjing `AppID`、`SecretKey`，创建客户端和轮询调度器。完整链路如下：

```text
POST /api/tasks
  -> handlers.createTask 校验 script 与参数、补默认值
  -> Chanjing.GetToken()
  -> POST https://open-api.chanjing.cc/open/v1/access_token
  -> data.access_token（本地缓存 50 分钟）
  -> Chanjing.CreateVideo(token, CreateVideoRequest)
  -> POST https://open-api.chanjing.cc/open/v1/create_video
  -> data（provider task id 字符串）
  -> 保存 data/tasks.json 的 Task{task_id, status:"pending", ...}
  -> scheduler.Add(task_id, token)
  -> Chanjing.GetVideoStatus(token, task_id)
  -> GET https://open-api.chanjing.cc/open/v1/video?id=<task_id>
  -> status / queue_status / progress / video_url
  -> store.UpdateTask(...)
```

`main.go` 启动时还会调用 `Scheduler.Restore`，恢复 `tasks.json` 中 `pending` 或 `generating` 的任务。浏览器可以通过 `GET /api/tasks` 查看本地任务列表，通过 `GET /api/tasks/:id` 查看本地记录；`POST /api/tasks/:id/refresh` 会立即向 provider 查询一次，`POST /api/tasks/:id/retry` 会用原参数重新提交一个新任务。

### Submit 请求参数与 provider task id

浏览器 `POST /api/tasks` 的输入字段为：

```text
script
avatar_id
audio_man_id
speed
volume
bg_color
resolution
subtitle_enabled
figure_type
drive_mode
backway
language
compliance_watermark
```

handler 将其转换为 `chanjing.CreateVideoRequest`：

```json
{
  "person": {
    "id": "<avatar_id>",
    "x": 0,
    "y": 0,
    "width": 1080,
    "height": 1920,
    "figure_type": "stand_body",
    "drive_mode": "random",
    "backway": 1
  },
  "audio": {
    "tts": {
      "text": ["<script>"],
      "speed": 1.0,
      "audio_man": "<audio_man_id>"
    },
    "wav_url": "",
    "type": "tts",
    "volume": 100,
    "language": "cn"
  },
  "bg_color": "#ffffff",
  "screen_width": 1080,
  "screen_height": 1920,
  "subtitle_config": {"show": false},
  "add_compliance_watermark": false,
  "source": 1,
  "audio_source": 0
}
```

`resolution=720P` 时尺寸变为 `720x1280`，其他值使用 `1080x1920`。`audio_source` 来自 `data/voices.json` 中与 `audio_man_id` 匹配的人声配置，并以顶层字段发送；系统人声时该字段因 `omitempty` 可以省略。Chanjing `create_video` 响应结构为 `{code, msg, data}`，代码只在 `code == 0` 时接受 `data`，并把它作为 `store.Task.TaskID` 和后续查询所用的 provider task id。当前没有单独的 generation id 或 provider/local 映射表。

### 状态查询、完成 URL 与本地状态

`Client.GetVideoStatus` 调用 `GET /open/v1/video?id=<task_id>`，响应结构为：

```json
{
  "code": 0,
  "msg": "success",
  "data": {
    "status": 30,
    "progress": 100,
    "video_url": "https://res.chanjing.cc/v.mp4",
    "msg": "success",
    "duration": 58000,
    "id": "<task_id>",
    "queue_status": "completed",
    "queue_desc": ""
  }
}
```

真实字段为 `data.status`、`data.progress`、`data.video_url`、`data.msg`、`data.duration`、`data.id`、`data.queue_status`、`data.queue_desc`。当前 scheduler 的判定为：

```text
status == 30 或 queue_status == "completed"
  -> 本地 status = "done", progress = 100, video_url = data.video_url

status >= 40 或 queue_status == "failed"
  -> 有 msg/queue_desc：本地 status = "failed", error = 原因
  -> 没有原因：最多保留 3 次空原因重试，期间仍为 "generating"

其他状态（包括 provider 的 10 生成中）
  -> 本地 status = "generating", video_url 清空
```

完成时的 URL 只来自 provider 的 `data.video_url`，由 scheduler 回调写入 `data/tasks.json` 的 `video_url`；没有本地视频转存或 URL 签名处理。`store.Task` 同时保存 `task_id`、`status`、`progress`、`video_url`、`error`、`script`、`params`、`created_at`、`completed_at` 和 `poll_count`。

### 错误码、超时与 provider 是否已受理

- Token、创建和状态接口都解析 provider 的 `{code, msg, data}`；`code != 0` 会返回包含 `code`、`msg` 和原始响应体的 Go error。代码没有将 provider code 映射成独立的业务错误枚举。
- `chanjing.Client` 的 HTTP client 超时为 30 秒。创建请求发生传输错误、读取失败或响应 JSON 无法解析时，`CreateVideo` 只返回错误，不返回 task id；handler 返回 HTTP 503 的“创建视频任务失败”。因此当创建请求超时或连接中断时，现有代码无法判断 provider 是否已经受理，也没有凭请求号查询或自动对账机制。
- scheduler 每个任务最多轮询 30 次，间隔从 2 秒开始按指数增长并封顶 30 秒；达到上限后删除内存 job，并将本地任务更新为 `status = "timeout"`。只要本地还保留 provider task id，仍可调用 `/api/tasks/:id/refresh` 手动查询，但不会自动执行超时后的 reconciliation。
- 状态查询遇到包含 `AccessToken已失效` 或 provider code `10400` 的错误时，会清空 token 缓存、重新取 token 并重试一次；其他查询错误会让该任务进入 `failed`。
- 浏览器层 HTTP 错误包括：请求/稿件校验 400，未知任务 404，删除进行中任务 409，读取或保存本地任务/人声配置失败 500，Chanjing 认证、提交或刷新失败 503。任务记录自身使用 `pending`、`generating`、`done`、`failed`、`timeout` 五类状态。

### 重复提交保护现状

当前没有 authorization、idempotency key、请求去重、generation record 或 provider task 映射表：

```text
同一 POST /api/tasks 重复到达 -> 每次都调用 create_video
POST /api/tasks/:id/retry -> 无论旧任务是否仍在 provider 运行，都创建新的 provider task id
创建成功后本地 SaveTasks 失败 -> provider 任务可能已受理，但本地没有可靠记录
```

`scheduler.jobs` 只按 task id 管理轮询，不构成提交幂等保护；本 Task 不新增这些机制。

### 已有契约验证

`chanjing/chanjing_test.go` 已验证 `CreateVideoRequest` 的 person/audio/TTS 嵌套字段、`source` 和顶层 `audio_source` 序列化，以及 `status=30`、`video_url`、`queue_status` 的响应解析。`scheduler/scheduler_test.go` 已验证成功完成、4X 失败、空失败原因重试、手动刷新、过期 token 重试和轮询参数 `maxPolls=30`。因此当前可复用的稳定业务链是：

```text
submit -> provider_task_id -> poll/status -> video_url
```

本 Task 没有修改 Chanjing API、任务状态语义或调度代码。

## Data & Config 审计（Task 0.5）

本节确认当前项目没有 SQL 数据库；现有持久化是运行目录下的 JSON 文件、进程内缓存和浏览器 `localStorage`。本节只记录数据边界，不新增 SQLite 或迁移代码。

### SQLite、数据库表与迁移机制

仓库中未发现 `database/sql`、SQLite 驱动、ORM、`CREATE TABLE`、`.sql` schema、迁移目录或 migration runner。`go.mod` 只有 Go 运行时和 `github.com/google/uuid` 间接依赖，没有数据库依赖；工作区也没有 `.db`、`.sqlite`、`.sqlite3` 文件。

因此当前 SQL 表数量为 0，现有数据库文件位置为“无”。也没有版本化 schema、迁移历史、回滚脚本或跨进程数据库锁。所有业务文件均由 `store` 包通过 JSON 编解码读写。

### 运行目录与现有数据文件

`main.go` 使用 `os.Executable()` 的目录作为运行根目录，在其下创建 `data/`；不是根据当前 shell 工作目录定位。首次启动只在文件不存在时创建以下文件：

```text
<可执行文件所在目录>/data/config.json
<可执行文件所在目录>/data/tasks.json
<可执行文件所在目录>/data/topics.json
<可执行文件所在目录>/data/avatars.json
<可执行文件所在目录>/data/voices.json
```

这些文件被 `.gitignore` 的 `data/` 规则排除，不属于 Git 版本库。当前工作区样本在审计时包含 3 条任务记录、0 条已保存选题、45 条形象记录和 2 条人声记录；这些数量属于本机运行状态，后续运行可能变化。

### 现有“表”与字段用途

虽然没有 SQL 表，但可以把这些 JSON 集合视为 Go capability-owned 的本地数据集合：

| 文件 / Go 类型 | 真实字段 | 当前用途 | Agent 边界 |
|---|---|---|---|
| `config.json` / `store.Config` | `chanjing.app_id`、`chanjing.secret_key`；`llm.api_url`、`llm.api_key`、`llm.model`；`search.provider`、`search.api_key`；`skill.source`、`skill.local_path`、`skill.github_url`、`skill.github_cache_until` | 应用和 provider 凭据、模型、搜索和 Skill 来源配置 | Agent 不直接读写原始密钥；通过 Go capability 或专门的配置注入使用 |
| `tasks.json` / `store.Task` | `task_id`、`status`、`progress`、`video_url`、`error`、`script`、`params`、`created_at`、`completed_at`、`poll_count` | 数字人提交记录、轮询状态、结果 URL、失败信息和提交参数 | 可读取任务状态和结果；不能作为 Agent 的 Draft、Version、Authorization 或 Generation Record 真相 |
| `topics.json` / `store.SavedTopic` | `title`、`type`、`source_url`、`raw_info`、`created_at`、`status` | 保存搜索后人工加入的选题；`raw_info` 保留结构化招聘事实，按 `source_url` 去重 | 可作为历史候选的只读来源或一次性导入源；写入仍由 Go `/api/topics` 负责 |
| `avatars.json` / `store.Avatar` | `id`、`name`、`preview_url` | 本地数字人目录；启动时从嵌入图片初始化，`GET /api/avatars` 还会合并 Chanjing 可用定制形象 | 作为只读形象目录；不承载 Agent 业务状态 |
| `voices.json` / `store.Voice` | `id`、`name`、`gender`、`description`、`preview_audio_url`、`audio_source` | 本地人声目录；创建视频时根据人声 ID 查找 `audio_source` | 作为只读人声目录；不承载稿件或会话数据 |

`tasks.json` 的 `params` 是任意 JSON，当前实际保存的是 `chanjing.CreateVideoRequest` 的嵌套参数；它没有独立列、外键、版本号或查询索引。`topics.json` 的 `raw_info` 同样是嵌入的结构化 JSON，不是可迁移的关系表。

### 文件读写、历史与并发边界

`store` 包使用进程内全局 `sync.RWMutex` 保护 JSON 读写；`UpdateTask` 在同一进程内执行读取、修改、写回，`AddTopic` 也在锁内按 `source_url` 去重。写入使用 `os.Create` 直接截断并重写，没有临时文件 rename、fsync、文件锁或跨进程事务。因此这些 JSON 文件适合单个 Go 进程的本地状态，不适合 Python Agent 与 Go 同时直接写入。

现有历史能力只有：

- `tasks.json` 保存视频任务当前状态和部分已完成记录；`GET /api/tasks` 会清理超过 30 天的终态记录，`DELETE /api/tasks` 会清空终态记录，进行中的任务不能删除。
- `topics.json` 保存人工加入的选题和搜索事实；没有版本、审核、分支或候选批次历史。
- `script` 只保存提交视频时的稿件快照；没有 Draft、Draft Version、局部修改记录或 Review 历史。
- `avatars.json` 和 `voices.json` 是最新目录快照，不是资源变更历史。

### 配置、密钥与缓存

生产 Go 服务的配置来源只有 `<exe_dir>/data/config.json`，没有读取 `.env` 或环境变量作为运行配置。`GET /api/settings` 会把三个 API Secret 返回为 `store.MaskedValue`；`PUT /api/settings` 收到掩码值时保留磁盘中的旧密钥，并要求 Chanjing AppID/SecretKey 非空后调用 `store.SaveConfig`。

`store.SaveConfig` 会把 Chanjing SecretKey、LLM API Key 和搜索 API Key 加密为 `ENC:` 前缀的 AES-GCM 文本；密钥由当前机器 hostname 派生。`LoadConfig` 解密，兼容没有 `ENC:` 前缀的历史明文，并在下一次保存时加密。这个方案没有外部 Secret Manager、密钥轮换或跨机器迁移能力；复制到另一台主机时可能无法解密。仓库里的 `scripts/debug_chanjing_api.py` 仅是调试脚本，可通过 `CHANJING_SK` 环境变量提供 SecretKey，不是生产服务的配置入口。

当前缓存不属于业务数据库：

```text
Chanjing access token：Client 内存，50 分钟，进程退出即丢失
Scheduler jobs：Scheduler 内存 map，重启后从 tasks.json 恢复 pending/generating
GitHub Skill：os.TempDir()/koubo-skill-cache-<hash>，配置的 github_cache_until 控制 24 小时缓存
浏览器偏好：localStorage，保存最近稿件、选题搜索历史、形象/人声和页面参数
```

前端 `SecureStore` 也写浏览器 `localStorage`，使用内置浏览器密钥对设置页的临时敏感字段做 AES-GCM 包装；它不是服务端配置真相，服务端仍以 `config.json` 为准。`artifacts/` 下的搜索 trace 和 Responses 探针输出是实验/诊断产物，不是运行时业务存储。

### Data boundary 与 Phase 1 Business DB 决策

当前没有可复用的 SQL 表，因此 **Agent Business DB 应新建独立 SQLite 数据库，不复用或改造 `data/*.json`**。Go 现有文件继续由 Go capability 读写，Python Agent 通过后续 Internal API 获取所需能力和结果；不允许两个服务共享 JSON 写入。

可复用与只读边界如下：

```text
可复用（通过 Go capability / Internal API）
  - topics.json：历史候选和 raw_info，可作为只读读取或一次性导入源
  - avatars.json：数字人形象目录
  - voices.json：人声目录
  - tasks.json：provider task 状态、进度、错误和 video_url 的只读查询

只能读取或由 Go 代写
  - config.json：Agent 不直接读取原始 Chanjing/LLM/Search 密钥
  - tasks.json：Agent 不直接更新 provider 状态，不把它当作 generation record
  - topics.json：Agent 不绕过 Go 去重逻辑直接写文件

必须在新 Agent DB 建立
  - ContentTask / candidate batch
  - Draft 与 Draft Version
  - Review Result
  - Generation Authorization / Generation Record / idempotency key
  - thread 与业务对象映射、长期内容记忆及审计事件
```

新库的物理路径、迁移工具和表结构留给后续 Phase 1；本 Task 只冻结边界：Go JSON 文件不是 Agent Business DB，配置密钥不是 Agent 状态，任务结果历史也不能替代稿件版本和生成授权历史。

## Phase 0 最终决策（Task 0.6）

第一版 Python ↔ Go Internal API Contract 已单独冻结在 [docs/internal-api-contract.md](./internal-api-contract.md)。Go 继续持有现有 JSON 数据和稳定 capability；Python 通过内部结构化 endpoint 调用搜索、写稿、数字人提交和状态查询。当前 public endpoint 与内部契约的映射、尚未实现的 wrapper、统一错误字段、`request_id`、`generation_id`、`provider_task_id` 和幂等边界均以该契约为准；本 Task 没有实现生产 API、安装 LangChain 或创建数据库。
