# Agent 升级独立工作区

本目录是从审计后的当前工作树复制出的升级工作区。交付用升级工作区位于原工作区之外，保留其源码、网页、可执行程序、运行配置、`data/` 数据和本地 Skill；本目录中的后续开发不得回写原目录。

旧版源码基线候选为 Git 提交 `7c49db4`（2026-09-18）。当前 HEAD 中的 Agent 代码保留在本副本中，供接入工作继续使用。旧版真实成功记录来自原目录的 `data/tasks.json` 与 `HANDOFF.md`，副本仅用于独立验证，不作为旧版数据的替代品。

## 运行隔离

- 升级版 Go 默认监听 `127.0.0.1:8909`，旧版继续使用 `8899`。
- 启动脚本把 Go 数据和 Agent 数据放在系统临时目录下的 `koubo-video-tool-agent-upgrade/`，也可以通过 `KOUBO_DATA_DIR` 和 `DB_PATH` 指定其他升级版目录。
- `build-upgrade.bat` 只输出 `koubo-video-tool-agent-upgrade.exe`，不会覆盖旧版的任何 `.exe`。
- Agent 默认通过 `http://127.0.0.1:8909` 调用升级版 Go capability；Go 通过 `AGENT_SERVICE_BASE_URL` 调用 Agent 服务，默认仍为 `http://127.0.0.1:8090`。
- 升级版默认不创建桌面快捷方式，也不自动打开浏览器。需要时显式设置 `KOUBO_CREATE_DESKTOP_SHORTCUT=1` 或 `KOUBO_OPEN_BROWSER=1`。

升级版的程序输出、端口和数据目录不得指向原工作区。是否将升级版替换为日常使用版本，由用户在独立真实验证后决定。
