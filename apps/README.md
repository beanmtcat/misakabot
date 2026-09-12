# Applications

- `audit-miniapp/` — Telegram 入群审核前端。
- `bot/` — MisakaBot 的 Python 服务、测试、工具及容器构建文件。
- `emby-manager/` — Emby 用户、播放记录及追更管理服务。

根目录仅保留共享部署配置；各应用的构建上下文由根目录 `compose.yaml` 统一声明。
