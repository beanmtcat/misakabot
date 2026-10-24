# Misaka Guard

Telegram 群广告与群控账号审核机器人。当前实现提供：

- 首发消息文字归一化（火星文分隔符、零宽字符、链接与 `@username`）；
- 可疑消息立即删除和临时限制；
- 本地开发判定器与 OpenAI 兼容大模型判定接口；
- 高置信广告自动永久封禁，边界事件进入人工复核；
- PostgreSQL 审计日志（本地开发与测试兼容 SQLite）；
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

将 `AUDIT_WEB_APP_URL` 配置为审计页的 HTTPS 地址（例如 `https://dashboard.example/audit/`）后，当前群的 Telegram 管理员可在目标群发送 `/audit`。Bot 会删除该命令，并在私聊发送带群组上下文的审计 Mini App 按钮。管理员须先私聊 Bot 并发送一次 `/start`，否则 Telegram 不允许 Bot 主动私聊。审计 API 自身仍校验 Telegram 签名，并允许 `ADMIN_USER_IDS` 中的管理员或当前群管理员访问受管群数据；这不授予非群管理员调用群命令的权限。

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

群组应将机器人设为管理员，并授予删消息、限制成员、封禁成员和邀请成员权限；在 BotFather 关闭 Group Privacy，确保机器人能收到普通消息与成员加入事件。直接入群会先限言并在群内发送行业黑话选择题；开启“需批准加入”的邀请链接时，则先通过私聊 Mini App 验证后批准入群，再完成群内选择题。

入群二次验证默认在题卡中显示 `@username + 用户 ID`；没有用户名时显示昵称和用户 ID。昵称会去掉控制字符、链接、域名和 `@`，最多保留 20 字；用户名和昵称都以等宽文本显示、不可点击，避免 Bot 替广告号展示可点击的推广信息。昵称优先取自入群事件或入群申请记录，取不到时只查询一次；查询失败仍显示用户 ID 并正常发送验证。设置 `SECONDARY_VERIFICATION_SHOW_USERNAME=false` 时只显示用户 ID。

二次验证题发送失败时，成员保持待验证，后台会重试；未成功记录题卡不会触发答题超时移除。为避免与正在进行的 Telegram 请求冲突，发送尝试超过两分钟后才进入恢复队列，后台每 30 秒扫描。题卡送达时才开始答题计时；超过原截止时间 30 分钟仍无法送达（例如成员已退群或 Bot 失去发言权限）时停止重试，按超时处理。管理员已完成的验证不会被恢复任务重新开启。

答错后会更换题目并保留原截止时间，题卡显示明确的 UTC+8 截止时间。编辑失败时转入持久化重发流程，已用次数不会清零；恢复期间不按原来的两分钟答题超时移除，重发成功后重新计时。已取得消息 ID 的未登记题卡有持久化清理兜底，正常登记会在同一事务中取消清理。Telegram 已接收消息但尚未返回消息 ID 就断线的情况无法由 Bot API 精确定位，因此不保证跨网络故障的绝对一次性投递。

二次验证超时、三次答错或管理员拒绝时，默认移出并封禁 1 小时。Compose 中的 `SECONDARY_VERIFICATION_BAN_HOURS` 可设为整数小时，`0` 表示永久封禁；有效范围为 `0..8784`，避免超过 Telegram 的限时封禁上限后被意外当成永久封禁。限时封禁到期后，用户可重新申请入群，仍须重新完成验证，不会自动回群。此设置不改变广告封禁或已有黑名单的永久封禁行为。

`/audit`、`/unban`、`/join_guard` 和 `/memory` 只在受管群的 Telegram 管理员命令菜单中显示，也只对当前群管理员生效。普通成员手动发送这些命令（包括“猫猫 /memory …”）时，消息照常经过广告审核，随后被 Bot 静默删除，不在群内回复任何提示。

若需要恢复误封用户，由当前群的 Telegram 管理员在受管群发送 `/unban <Telegram 用户 ID>`，或回复目标用户消息发送 `/unban`。该命令会同时解除本群 Telegram 封禁和该用户的本地永久黑名单；用户之后需重新申请入群，才会再次收到私聊验证。

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
KIMI_MODEL=k3.1-pro    # 审核与群聊回复使用同一模型
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

第一阶段 Turnstile 通过并自动批准入群后，Bot 会在群内发起第二阶段行业黑话题：从 `src/misakabot/join_questions.py` 的题库中随机抽取一道，选项顺序也随机。题库涵盖服务器、商家别称、PT、网络线路等词汇，只考常用含义，不把商家口碑、免费套餐政策或线路性能承诺作为标准答案。挑战仅绑定当前 Telegram 用户，正确答案保存在服务端。每答错一次，题卡会换成一道新题，旧按钮（包括管理员按钮）随即失效，答题倒计时不重置；这样无法靠排除法逐个试选项，纯随机猜三次的通过率约 58%（若同一题可重复作答则为 75%）。答错三次或超时按 `SECONDARY_VERIFICATION_BAN_HOURS` 移出并封禁；通过后才解除禁言。

编辑题库时，`_question("题目", "标准答案", "干扰项一", "干扰项二", "干扰项三")` 的第二个参数就是标准答案。每题保留四个不同选项；发送题卡时才随机打乱，不需要手动调整答案位置。

完整架构和入群审核策略见 [ADS_MODERATION_DESIGN.md](../../docs/ADS_MODERATION_DESIGN.md)。

## 本地与部署配置

仓库根目录不会提交实际的 `compose.yaml`、`.env` 或 Emby 传输映射。首次部署可在根目录复制模板：

```bash
cp .env.example .env
cp compose.example.yaml compose.yaml
```

填写 `.env` 后再运行 Compose。实际生产配置仅保留在部署主机，不应提交到 GitHub。
