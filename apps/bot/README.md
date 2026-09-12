# Misaka Guard

Telegram 群广告与群控账号审核机器人。当前实现提供：

- 首发消息文字归一化（火星文分隔符、零宽字符、链接与 `@username`）；
- 可疑消息立即删除和临时限制；
- 本地开发判定器与 OpenAI 兼容大模型判定接口；
- 高置信广告自动永久封禁，边界事件进入人工复核；
- SQLite 审计日志；
- 供 Telegram Mini App 使用的管理员审计 API，服务端校验 Web App `initData`。
- 入群申请自动验证：验证通过才放行；入群后无论潜伏多久，首次发言均进入强化审核状态。

## 后端代码结构

`src/misakabot/main.py` 只负责配置、依赖组装和启动，两个 CLI 入口保持不变。

- `gateway.py`：Telegram 操作与管理员缓存。
- `bot_commands.py`：启动时注册 Bot 命令菜单。
- `handlers/`：路由组装、管理员操作、命令、入群事件及普通消息审核。
- `group_reply.py`：群内 AI 回复、上下文、长期记忆与输入状态。
- `webhook.py`：Webhook 认证、去重、生命周期及首次验证 HTTP 接口。
- `workers.py`：验证超时和后台清理任务的调度。
- `onboarding.py`、`service.py`、`repository.py`：分别负责入群状态、审核业务与数据库访问。

路由顺序固定为命令 → 入群事件 → 普通消息，避免普通消息处理器抢先消费命令或入群通知。
日志中的模块名随拆分变化（例如 `misakabot.handlers.messages`），事件标识保留。

本地验证（不调用真实 Telegram 或 Kimi）：

```bash
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/basedpyright --pythonpath .venv/bin/python src/misakabot
.venv/bin/python -m unittest discover -s tests -q
```

## 本地启动

```bash
cp .env.example .env
# 编辑 .env，至少设置 TELEGRAM_BOT_TOKEN、ADMIN_USER_IDS 与 ALLOWED_GROUP_IDS
.venv/bin/misakabot
```

审计 API：

```bash
.venv/bin/misakabot-api
```

默认 API 地址为 `http://127.0.0.1:8080`。Mini App 调用 API 时必须使用 `X-Telegram-Init-Data` 请求头；后端会校验签名、有效期和管理员白名单。不要把 Bot Token 或 LLM Key 写进 Mini App 前端。

将 `AUDIT_WEB_APP_URL` 配置为审计页的 HTTPS 地址（例如 `https://dashboard.example/audit/`）后，受管群的项目管理员或 Telegram 群管理员可在目标群发送 `/audit`。Bot 会删除该命令，并在私聊发送带群组上下文的审计 Mini App 按钮。管理员须先私聊 Bot 并发送一次 `/start`，否则 Telegram 不允许 Bot 主动私聊。

`ALLOWED_GROUP_IDS` 是逗号分隔的 Telegram 群组 ID 白名单（超级群通常以 `-100` 开头）。未配置时机器人不会处理任何群的入群申请或消息，避免被意外拉入其他群后误执行删帖、限言或封禁。

## Docker

```bash
docker build -t misakabot:local .
docker run -d --name misakabot --env-file .env \
  misakabot:local
```

镜像默认启动 Telegram 长轮询机器人。启动管理员审计 API 时覆盖命令，并仅在受信任网络中暴露端口：

```bash
docker run --rm --env-file .env -p 127.0.0.1:8080:8080 \
  misakabot:local misakabot-api
```

也可使用 Docker Compose：

```bash
docker compose up -d --build                  # 只启动机器人
docker compose --profile audit up -d --build  # 同时启动本机审计 API
```

日常更新请使用部署脚本并指定目标，避免无关服务被重启：

```bash
./deploy.sh bot    # 仅机器人（会同步 DMIT 知识库）
./deploy.sh audit  # 仅审计 API 与页面（会同步 DMIT 知识库）
./deploy.sh emby   # 仅 Emby 管理服务
./deploy.sh all    # 所有应用服务
```

Compose 的配置值直接写在 [compose.yaml](compose.yaml)；启动前请把 `TELEGRAM_BOT_TOKEN`、群组与管理员 ID，以及所用大模型的 Key/模型名替换为真实值。不要将包含真实密钥的 Compose 文件提交到公开仓库。

运行中的服务加入现有 `docker_default` 网络；镜像构建阶段使用 Docker 的 `host` 网络模式。构建网络模式不能填写 Docker 网络名。

审计 API 仅映射到 `127.0.0.1:8080`；如需经由反向代理提供 Mini App 访问，请在代理层做 TLS 与访问控制，不要直接把 API 端口暴露到公网。

## Telegram Webhook

机器人默认以 Webhook 模式运行，并将 `https://dashboard.example/telegram/webhook` 注册给 Telegram。反向代理需将该精确路径转发给机器人容器的 `8081` 端口：同一 Docker 网络中的代理可使用 `http://misaka-bot:8081`；运行在宿主机的代理可使用 `http://127.0.0.1:8081`。该接口会校验 Telegram 的 `X-Telegram-Bot-Api-Secret-Token` 请求头。

群组应将机器人设为管理员，并授予删消息、限制成员、封禁成员和邀请成员权限；在 BotFather 关闭 Group Privacy，确保机器人能收到普通消息与成员加入事件。普通入群会先限言并在群内发送一次性验证按钮；开启“需批准加入”的邀请链接时，则使用私聊验证后自动批准。

若需要恢复误封用户，由 `ADMIN_USER_IDS` 中的管理员在受管群发送 `/unban <Telegram 用户 ID>`。该命令会同时解除 Telegram 封禁和本地永久黑名单；用户之后需重新申请入群，才会再次收到私聊验证。

“需批准加入”模式的验证页为 `/join-verify/`。在 Cloudflare Turnstile 创建此域名对应的小组件后，将公开的 Site Key 填入 `TURNSTILE_SITE_KEY`、服务器 Secret Key 填入 `TURNSTILE_SECRET_KEY`；前者可出现在浏览器，后者绝不可放入前端或公开仓库。

## 运行日志

机器人与审计 API 都会将启动、入群验证、每条受管消息的审核结果、隔离/封禁动作和 API 访问日志输出到容器标准输出：

```bash
docker compose logs -f bot
docker compose --profile audit logs -f audit-api
```

`LOG_LEVEL` 默认是 `INFO`；改为 `DEBUG` 可显示第三方库的调试日志。日志不会记录 Bot Token 或大模型 API Key，完整原文与裁决证据仍保存于审计数据库并由管理员页面查看。

## 大模型配置

本地开发默认：

```env
LLM_MODE=rule_based
```

接入兼容 `/chat/completions` 的服务：

```env
LLM_MODE=openai_compatible
LLM_API_BASE_URL=https://your-provider.example/v1
LLM_API_KEY=...
LLM_MODEL=...
```

使用 Kimi Coding Plan：

```env
LLM_MODE=kimi_coding
KIMI_API_BASE_URL=https://api.kimi.com/coding/v1
KIMI_API_KEY=...       # 仅保存在服务器 .env
KIMI_MODEL=...         # 填入该 Coding Plan 账号实际可用的模型名
BOT_REPLY_ENABLED=true # 仅在 @Bot 或回复 Bot 消息时调用 Kimi 回复
```

Kimi Code 与 Moonshot Open Platform 是两个独立入口；这里使用 Kimi Code 的 Coding API 基址。模型名不硬编码，避免把账号未开通的模型写死。入群验证通过只证明操作 Telegram 账号的控制权，并不把账号视为可信；程序控制账号可潜伏，因此首次实际发言才开始新成员的内容与行为画像审查。

当 `LLM_MODE=kimi_coding` 且 `BOT_REPLY_ENABLED=true` 时，Bot 只会在允许群组内被 `@Bot` 提及、或有人回复 Bot 的消息时调用 Kimi 生成简短回复。广告审核优先执行；消息被隔离、待复核或封禁时不会再获得聊天回复。

## DMIT 官方知识库

DMIT 中文文档知识库仅用于 `DMIT_KNOWLEDGE_GROUP_IDS` 配置的群组；例如：

```env
DMIT_KNOWLEDGE_GROUP_IDS=-1001234567890
# 可与知识库群不同；只有这些群的入群欢迎消息会带频道按钮。
DMIT_CHANNEL_GROUP_IDS=-1001234567890
DMIT_NEWS_CHANNEL_URL=https://t.me/dmitnews
```

在该群中，“大妈”会按 DMIT 理解。Bot 仅在被 `@` 或回复时，根据提问从本地文档快照检索相关段落并交给 Kimi；不会把文档用于其他群的对话或广告审核。快照覆盖当前官方中文文档导航下的实例、IP、计费、退款、工单、账户、推广、SSH 密钥及连接指引。

官方文档更新后，在源码根目录重新生成快照，再构建部署：

```bash
python3 tools/sync_dmit_knowledge.py
docker compose --profile audit up -d --build --force-recreate
```

同步脚本只访问 `https://docs.dmit.io/zh/`；用户提问时不会实时抓取网页。对实时库存、价格、订单、账户或工单状态，Bot 会提示以官网、控制台或工单为准。

入群申请通过 Mini App 自动审核；用户在 `JOIN_VERIFICATION_TTL_MINUTES`（默认 5）分钟内未完成验证时，Bot 会自动拒绝申请。内容审核不依赖用户是否为新成员：Bot 在某群首次看到某用户发言时必定交给 Kimi，之后仅在本地规则判为可疑时才送审。

第一阶段 Turnstile 通过并自动批准入群后，Bot 会在群内发起第二阶段 VPS 交易安全题：题目和选项顺序随机、挑战仅绑定当前 Telegram 用户、正确答案仅保存在服务端。答错三次或超时会自动移出群组；通过后才解除禁言。该题用于提高批量脚本的操作成本，不替代后续的广告与行为审核。

完整架构和入群审核策略见 [ADS_MODERATION_DESIGN.md](../../docs/ADS_MODERATION_DESIGN.md)。

## 本地与部署配置

仓库根目录不会提交实际的 `compose.yaml`、`.env` 或 Emby 传输映射。首次部署可在根目录复制模板：

```bash
cp .env.example .env
cp compose.example.yaml compose.yaml
```

填写 `.env` 后再运行 Compose。实际生产配置仅保留在部署主机，不应提交到 GitHub。
