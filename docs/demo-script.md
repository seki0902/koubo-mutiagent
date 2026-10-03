# 最终演示脚本（Phase 9 / Task 9.5 冻结）

本文档是 MVP 冻结后的最终演示脚本：只按 Case 走一遍，不再扩需求。
四个 Case 的对话内容、系统应做的事、可观察的判据都在这里，任何一次演示都按本文件执行。

---

## 0. 演示前准备

```powershell
# 终端 1：升级版 Go（Browser 只需访问它）
.\run-upgrade-go.ps1

# 终端 2：Python Agent 服务
.\run-upgrade-agent.ps1
```

浏览器打开 `http://127.0.0.1:8909/agent.html`，在右栏点「Agent 模型设置 → 展开」，
填入**服务商 / 模型名 / API Base URL / API Key**，点保存。

设置项存在与蝉镜、搜索设置同一个 `config.json` 的 `agent` 段，密钥沿用同一套
机器绑定加密；保存后**下一轮对话立即生效，不需要重启任何服务**。
（仍然支持环境变量方式：`SUPERVISOR_MODEL` / `SUPERVISOR_MODEL_API_KEY` 等。）

- 只填「模型名 + API Key」即可，服务商默认 `deepseek`，Base URL 留空用服务商默认。
- 如果之前填过密钥，输入框会显示「已保存（留空表示不修改）」，留空不会清掉它。
- 右侧面板与状态条来自后端事件与快照接口，不来自 Assistant 文本。

> 没填之前发消息会得到契约化错误
> （`supervisor model name is missing; set SUPERVISOR_MODEL`），
> 填错 Key 会得到 provider 的 401。两种都是有效的失败演示：链路是通的，
> 差的只是凭证。

---

## Case A｜正常链路

按顺序在聊天框输入（每句一条）：

```text
今天做啥好呢
第三个不错，但帮我看看现在还招不招
可以，写吧
第二段短一点
看看有没有风险
这个版本可以
生成吧
```

系统应做与可观察判据：

| 轮次 | 系统应做 | 可观察判据 |
| --- | --- | --- |
| 1 | Topic Agent 生成候选批次 | 状态条「选题」转为工作中→完成；出现 3～5 张选题卡片；右侧面板出现候选批次 |
| 2 | Research Agent 核验第 3 个候选 | 状态条「核验」工作中→完成；回答里给出可核验来源；证据不足时必须回答无法确认 |
| 3 | Script Agent 写 v1 | 状态条「写稿」工作中→完成；右侧面板出现稿件 v1 与正文 |
| 4 | Script Agent 局部改稿 | 右侧面板版本变为 v2，v1 仍然存在（版本只增不改） |
| 5 | Content Review 审稿 | 状态条「审核」工作中→完成；右侧面板出现审核结论，且绑定 v2 |
| 6 | 不含生成意图 | 不产生 generation 记录；回答只描述当前版本 |
| 7 | 明确授权后提交 | 状态条「数字人」工作中→完成；右侧面板出现 generation 记录与 provider task id |

验收要点：

```text
- 点击卡片与说「第三个」指向同一个 topic_id
- 每一次改稿都产生新版本，旧版本不被覆盖
- 授权前不产生 provider 提交；授权后才产生
- 重复说「生成吧」不会产生第二个 provider task
```

---

## Case B｜不确定事实

新会话，只输入：

```text
这个岗位现在还招吗？
```

系统应做：Research Agent 取证据；拿不到官方原始来源时**必须回答无法确认**，
不得把「没有关闭信息」当成「仍然开放」。

可观察判据：

```text
- 回答中出现「无法确认 / unknown」这类结论，而不是「还在招」
- 状态条「核验」走完 working → finished
- 右侧面板不产生 verified 结论（证据不足时不写成已核验）
```

---

## Case C｜恢复

1. 走完 Case A 的第 1～4 轮（至少到稿件 v2）。
2. **中途重启服务**：

```powershell
# 关掉两个服务进程，然后重新启动
.\run-upgrade-go.ps1
.\run-upgrade-agent.ps1
```

3. 刷新浏览器页面（`http://127.0.0.1:8909/agent.html`），继续输入：

```text
刚才那版继续改一下
```

可观察判据：

```text
- 刷新后右侧面板仍显示同一个 session 的选题、稿件版本与正文、审核、生成状态
- 继续改稿从重启前的版本继续递增（不回到 v1，也不产生第二个 v1）
- thread 历史仍在（同一 session_id 的会话可继续）
```

---

## Case D｜幂等

在 Case A 已经提交成功之后，再连续输入两次：

```text
生成吧
生成吧
```

可观察判据：

```text
- provider task 只有一个（右侧面板「历史提交」只有一行）
- 本地 generation 记录只有一条，generation_id 与第一次相同
- 不会出现第二个 provider_task_id
```

---

## 一键可执行部分（无需模型凭证）

以下两条脚本对 Case C 与 Case D 的地下管道做可执行验证：

```powershell
# 固定会话 + 逐字段检查（Case C 的后端部分）
$env:DB_PATH = "$env:TEMP\koubo-p9\agent.sqlite"
python scripts/seed_phase8_session.py
python scripts/check_recovery.py step0-before-restart

# 重启任意进程后再跑一次，应得到逐字段一致的结果
python scripts/check_recovery.py stepA-python-restarted
```

Case D 的幂等判据由自动化测试覆盖：

```powershell
python -m pytest agent_service/tests/test_generation_service.py -q          # 同一授权只提交一次
python -m pytest agent_service/tests/test_phase2_e2e.py -q                  # 重复生成请求复用同一 generation
python -m pytest agent_service/tests/test_failure_injection.py -q           # 超时/未知提交不重复调用 provider
```

---

## 演示禁忌

```text
不要为了演示顺手改代码
不要临时放宽预算上限（MAX_* / RECURSION_LIMIT）来“让演示更顺”
不要在演示中省略任何一轮，Case A 的七句是完整链路
```
