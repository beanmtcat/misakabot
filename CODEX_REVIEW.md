# 待验证：Claude 在 Codex 改动之上的修复（2026-10-23）

工作区里有两批改动，都没有提交：先是 Codex 未完成的那一批，然后是 Claude 在此基础上做的修复。本文只说明 **Claude 改了什么**，以及需要逐条验证的点。请审查行为是否正确、有没有引入新问题，不要只看测试是否通过。

## 怎么跑

```bash
# Bot（预期 154 项通过）
cd apps/bot && PYTHONPATH=src ../../.venv/bin/python -m unittest discover -s tests
# Bot 类型检查（预期 0 errors）
cd apps/bot && ../../.venv/bin/basedpyright --pythonpath ../../.venv/bin/python src
# Emby（预期 4 项通过；apps/emby-manager/.venv 是空的，要用根目录的 venv）
cd apps/emby-manager && PYTHONPATH=src ../../.venv/bin/python -m unittest discover -s tests
# 审计页
cd apps/audit-miniapp && npm ci && npm run build && npm run lint && npm audit --omit=dev
```

---

## 1. 普通成员的管理命令不能再绕过广告审核【安全】

**之前的问题**：命令路由排在审核路由前面。非管理员发 `/memory 加V…`、`/audit xxx` 或 `猫猫 /memory …`，处理器直接 `return`，消息就不会进入审核。

**改法**
- `handlers/commands.py`：遇到非管理员时，`/audit`、`/unban`、`/join_guard`、`/memory`、"猫猫 /memory" 都改为 `raise SkipHandler()`，让消息继续往下走；群里收到的 `/start` 也一样。新增 `is_management_command()`，能识别 `/cmd` 和 `/cmd@本Bot`，但不会识别 `@其他Bot`。
- `handlers/messages.py`：审核结果为 ALLOW 或 RELEASE、发送者不是管理员、消息又是管理命令时，静默删除这条消息，不调用 LLM 回复。审核结果为 NEEDS_REVIEW 时保留原消息，交给复核卡处理。
- `group_reply.py`：普通成员在记忆未开启时说"记住：…"，不再回复"请先发送 /memory on"。

**请验证**
- [ ] 非管理员发这四个命令、发 `猫猫 /memory on`、发 `/audit@本Bot`：都会经过 `service.moderate`，随后被删除，群里没有任何回复。
- [ ] `/audit@OtherBot` 不会被删除。
- [ ] 管理员发的命令照常生效，不经过审核，也不调用 LLM。
- [ ] `SkipHandler` 之后，onboarding 路由和 moderation 路由的回调处理器不会误接这条消息。
- [ ] 消息删除失败（Bot 没有删除权限）时只记日志，不抛异常。

## 2. 命令菜单改回只对管理员可见

- `bot_commands.py`：对每个受管群，先删除 `BotCommandScopeChat` 的菜单，再给 `BotCommandScopeChatAdministrators` 设置四个命令。README 已同步修改。
- [ ] 确认 Telegram 的作用域优先级：普通成员看不到菜单，群管理员能看到。

## 3. 题库：消除"选最长项"的偏差和答案泄露

- `join_questions.py`：33 道题的选项全部重写。之前有 21 道题的正确答案恰好是唯一最长的选项；"白嫖"和"4837"两题的答案里直接带着题目问的词。
- `tests/test_join_questions.py`：新增两项检查。一是固定选最长项、固定选最短项的命中率都不超过 30%（现在分别是 23% 和 16%）；二是正确答案不能单独重复题目里引号中的词。
- [ ] 人工核对每道题的标准答案在社区里确实是通行含义，干扰项都明确是错的。
- [ ] 检查有没有其他泄露答案的特征，比如语气、格式、只有正确答案带括号等。

## 4. 答错就换新题（三次机会不变）

- `onboarding.py`：调用 `verify_group_challenge` 前先预生成下一题。答错且还没用完三次时，在同一个数据库事务里写入新的 `token_hash` 和 `answer_index`，旧按钮（包括管理员按钮）立即失效。随后调用 `gateway.update_group_vps_challenge` 把题卡原地改成新题，答题倒计时不重置。
- `repository.resolve_group_challenge`：新增参数 `next_token_hash` 和 `next_answer_index`。
- `gateway.py`：把拼题卡文本、按钮和成员标签的代码抽成私有方法，新增 `update_group_vps_challenge`（同步改了 Protocol）。
- `handlers/onboarding.py`：把点击者的 username 和 full_name 传进去，改题卡时不用再查接口。
- 理论值：纯随机猜三次，通过率从 75% 降到 1 − (3/4)³ ≈ 58%。
- **已知取舍**：新题可能和上一题相同（约 1/33 的概率，选项会重新打乱）。为了不改数据库结构，没有排除这种情况。
- [ ] 并发：同一个成员快速点两次，第二次不能被算作第二次答错。
- [ ] 题卡编辑失败时，新题已经存好，成员会按超时被移出。确认这样处理可以接受。
- [ ] SQLite 和 PostgreSQL 两种后端下，这条 UPDATE 的语义一致。

## 5. 发题失败不再无限重试

- `repository.py`
  - `create_group_challenge(only_if_undelivered=True)` 不再修改 `verification_expires_at`，保留原来的截止时间作为锚点。
  - `set_group_challenge_message_id` 新增必填参数 `expires_at`：题卡真正发出去时才开始答题计时。
  - `undelivered_group_challenges` 新增 `delivery_deadline`。`expired_pending_verifications` 和 `claim_expired_verification` 也新增 `delivery_deadline`：未送达的题卡过了截止时间 30 分钟后，交给超时流程处理（封禁或移出）。
- `onboarding.py`：新增 `UNDELIVERED_CHALLENGE_GRACE_MINUTES = 30` 和 `_undelivered_challenge_deadline()`。
- **行为变化**：直接入群时，计时从题卡发出时开始，而不是从 `joined_at` 开始。`test_expired_direct_join_is_banned` 已按这个规则更新。
- [ ] 字符串时间比较依赖所有时间戳都是 UTC 的 isoformat，确认没有混入其他格式。
- [ ] `delivery_deadline` 的默认值是 `""`，这时未送达的行永远不会被清理，确认生产代码的所有调用都传了这个参数。
- [ ] 进程正好在"题卡已发出、消息 ID 还没写入"之间崩溃时，2 分钟后会补发一张新卡，旧卡留在群里。这是已知的边缘情况，没有修。

## 6. 题卡上的名字：不可点击、经过清理、少查接口

- `gateway.py`：新增 `sanitize_display_name()`，会去掉控制字符、链接、常见域名和 `@`，最多保留 20 字。用户名和昵称都放在 `<code>` 里，题卡改用 `parse_mode="HTML"`，题目文本做了 `html.escape`。
- 昵称的来源依次是：入群事件的 `full_name`（`DirectJoinInput.display_name` 是新增字段）→ `join_request_events` 表（新增 `latest_join_request_display_name`）→ 调一次 `get_chat_member`（3 秒超时，不重试）。
- [ ] 题卡里所有可能来自用户的文本都经过了转义，HTML 不会解析失败（否则 Telegram 会拒绝整条消息）。
- [ ] 正则不会把正常中文昵称误删得太多。

## 7. Emby：不再因为 404 误归档整批剧集

- `emby_client.list_episodes_for_series`：收到 404 后再调一次 `get_series`，只有抛出 `EmbyItemNotFoundError` 才认定剧集已删除，其他情况一律重新抛出原错误。
- `service.sync_series`：缺失剧集数 ≥ 5 且超过追更总数一半时，抛出 `RuntimeError` 并中止同步，不归档。
- [ ] `get_series` 对已删除的剧集确实返回 200 和空 Items（而不是 404），否则这层确认永远不会成立。
- [ ] 中止同步抛出的异常会被调用方正确处理，不会让整个定时任务崩溃。

## 8. 其他清理

- `onboarding.py`：删掉了重复的封禁时长校验（`config.py` 和 `gateway.ban_member` 里各保留一处）。
- `webhook.py`：两段 `except` 合并，"按钮已过期"的判断移进通用分支，行为不变。
- 审计页
  - `app/page.tsx`：新增 `errorDetail()`，修掉 3 处 `response.json()` 返回 `unknown` 导致的类型错误。
  - `package.json`：`@cloudflare/workers-types` 改为 `5.20261007.1`，不再用昨天刚发布的版本；新增 `overrides: { "fflate": "0.8.3" }`，清掉中等风险漏洞。
  - [ ] 确认 fflate 0.8 与 satori / `@vercel/og` 兼容（本项目似乎没有用到 OG 图片生成）。

## 已知没修 / 不在本次范围

- 处理器一开头调用 `callback.answer()` 时，如果报"按钮已过期"，这次点击会被丢掉，需要成员重新点一次。
- shadcn 自带的 `components/ui/chart.tsx` 有 3 处 `restrict-template-expressions` 的 lint 告警，这个文件没有改过，不影响构建。
- 审计页还有 6 个高风险漏洞，来自 vinext 和 shadcn 依赖链里的 `braces` / `micromatch`，上游没有修复版本。
- 没有部署，也没有在真实 Telegram 群里验证过。

---

## Codex 修复与复查记录（2026-10-23）

以上是修复前的审查说明，当前结果如下：

- Bot 162 项测试通过；Emby 4 项测试通过。
- Bot basedpyright 为 0 errors、688 warnings，完整命令因现存警告退出 1；没有关闭或降低检查规则。
- 审计页 `npm ci`、`npm run lint`、`tsc --noEmit`、`npm run build` 均通过。workers-types 已调整为满足 Wrangler peer 要求的 5.20261019.1。
- 无头浏览器使用本地模拟 Telegram 身份与审计数据，验证了鉴权入口、静态资源/水合、移动详情弹窗、Esc 关闭、筛选切换丢弃迟到分页，无浏览器 JS 错误；没有操作真实群成员。
- PostgreSQL 连接级临时表验证了换题防重放、失败重发、次数保留、超时排除、消息登记 CAS、旧卡清理及管理员跨群 token 拒绝；正式成员数据未写入。
- 菜单清理补上了 Default 及中英文历史作用域，避免删除 Chat 菜单后回退显示旧命令。
- 编辑题卡前暂时转为未登记状态，避免网络等待过程中被超时任务移除；编辑失败进入已有持久化重发流程，次数不会清零。正常换题保留原截止时间，显示绝对 UTC+8 截止时间。
- 已取得 ID 的题卡立即写入持久化清理兜底，登记成功时同一事务取消；编辑失败的旧卡一分钟后清理。Telegram 已接收但尚未返回消息 ID 就断线的极端窗口无法由 Bot API 精确定位，不保证绝对一次性投递。
- “小鸡”题移除了正确项独有的括号，并加入防回归检查。
- `fflate` 基础压缩及 satori SVG 冒烟通过；没有 OG 动态生成路由，未将 Node 下 OG 模块加载问题归因于 fflate。
- `npm audit --omit=dev` 仍报告文档原已列出的 6 个上游高风险依赖项。没有使用强制降级至 vinext 0.0.15 的方式绕过，不将它们描述为已修复。

## 类型警告清理与复查（2026-10-24）

上述 688 条警告及 6 项依赖告警是 10 月 23 日的历史检查结果，本次结果如下。

### 实现

- 将 SQLite / PostgreSQL 适配器抽到 `apps/bot/src/misakabot/database.py`，明确连接、游标、结果行和参数类型；SQLite 行形状经过运行时校验，PostgreSQL 保留原生时间、JSONB、布尔值。没有修改表结构、参数占位符或事务锁策略。
- 仓储边界验证整数、可空用户名及可空时间字段，处理保存后查询结果为空的情况；PG 防护时间在返回字符串的接口中统一使用 ISO 格式。通用查询仍保留原生字段值。
- 模型响应使用结构化校验，`is_ad` 必须是真正的布尔值，置信度必须是 0～1 的数值，证据必须是字符串数组。修复旧代码 `bool("false") == True` 的误判风险；格式不符时继续交给既有异常/人工复核流程，不据此封禁。
- Telegram 身份和外部 JSON 补齐验证边界；管理员接口收到异常响应时仍然拒绝授权。没有用户名的合法用户仍可通过身份验证。
- FastAPI 参数使用 `Annotated`，保留鉴权依赖、查询约束、请求头别名；回调使用显式注册，保留路由顺序和过滤条件。
- 补齐类属性类型，明确丢弃不需要的调用结果，去除无效空值判断与隐式字符串拼接歧义；没有降低检查规则、添加 `type: ignore` 或用 `cast` 掩盖问题。
- 显式声明 `typing-extensions`，将 Pydantic 最低版本提高到包含 `JsonValue` 的 2.5，并同步锁文件；未更换已锁定的依赖版本。

### 验证结果

- Bot：发现 178 项，本地执行的 174 项全部通过，4 项真实 PG 集成测试在未设置测试 DSN 时跳过。
- 真实 PostgreSQL：上述 4 项集成测试已在现有容器的独立进程中运行并全部通过，覆盖原生字段/游标、会话记忆与防护时间、答题换题/防重放/管理员跨群拒绝、审计 JSONB/SQL 筛选/分页/汇总。所有写入都在连接级临时表中；未运行生产建表或修改正式记录。
- `basedpyright --pythonpath ../../.venv/bin/python src`：**0 errors、0 warnings、0 notes，退出码 0**；指定 `--pythonversion 3.11` 也通过。
- `uv lock --offline --check --python ../../.venv/bin/python`：通过（指定根目录虚拟环境和可写临时缓存）。
- Emby：4 项测试通过。
- 审计前端：`npm run lint`、`npx tsc --noEmit`、`npm run build` 通过；`npm audit --omit=dev --json` 返回 **0 漏洞**。此前 6 项告警已由上游撤回，不是本次通过忽略规则或强制降级消除。
- `git diff --check` 通过。仍有上游 Vinext 的 sourcemap/分块提示及 Starlette 测试客户端的弃用提示；它们与本次清零的 Python 静态类型警告不同。
- 上述为发布前检查；后续发布状态见下节。测试未操作真实群成员。

复跑真实 PG 测试（可在测试数据库执行，脚本自身仅创建临时表）：

```bash
cd apps/bot
MISAKABOT_TEST_DATABASE_URL='postgresql://用户:密码@主机:5432/数据库' \
PYTHONPATH=src:tests ../../.venv/bin/python -m unittest test_postgres_integration -v
```

## 发布验证（2026-10-24）

- 发布本次类型清理、结构化响应校验及验证文案调整；群消息、验证结果和题目中不再出现“行业黑话”，题库答案、验证次数和超时逻辑不变。
- 已重建并重启 `misaka-bot`、`audit-api`，启动完成且重启计数均为 0；未重启 `audit-ui`、`emby-service` 或数据库。
- Bot 命令菜单及 webhook 注册成功；两个容器内各 31 个运行文件（含知识库资源）的 SHA-256 与本地待提交版本一致。
- Bot、审计 API 的本机健康接口均返回 200；公开 `/audit-api/healthz` 和 `/audit` 返回 200；未携带 Telegram 身份访问审计事件接口返回 422。
- 部署前再次执行 Bot 测试：174 项通过、4 项 PG 集成测试按环境配置跳过；类型检查仍为 0 错误、0 警告。真实 PG 的 4 项临时表测试已在本次发布前获授权复跑并全部通过。
- 按部署脚本同步了 17 篇 DMIT 文档；与上一版按 URL 对比无新增、删除或正文变化，只有顺序及同步时间变化。
- 新文案仅对新发送或随后编辑的消息生效，本次未批量修改、删除群内历史消息。
