# MisakaBot monorepo

本仓库包含独立部署、独立构建的应用与插件：

- [Bot 服务](apps/bot/README.md)
- [Telegram 审核前端](apps/audit-miniapp/)
- [Emby 管理服务](apps/emby-manager/README.md)
- [MoviePilot 插件](plugins/moviepilot-season-remap/README.md)

共享部署配置在根目录。首次配置请复制模板并填写本地密钥：

```bash
cp .env.example .env
cp compose.example.yaml compose.yaml
```

实际 `compose.yaml`、`.env` 和运行态数据均已忽略，不会被提交到 GitHub。

部署脚本同样分离：提交 [deploy.example.sh](deploy.example.sh)，本地复制为忽略的 `deploy.sh` 后通过环境变量传入主机、端口与私钥路径。
