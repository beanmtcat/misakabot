# 待修复：对 9c31d9f / 7ba12ac 的审查结论（2026-10-24）

审查范围是提交 `9c31d9f` 和 `7ba12ac`，审查时这两个提交已经部署。没有发现安全漏洞。下面按优先级列出问题；每条修完都要补测试，并在本文件末尾记录结果。

基线：Bot 178 项测试通过（4 项 PG 集成测试跳过），basedpyright 0 errors / 0 warnings，Emby 测试通过。修改后这些检查不能退化。

```bash
cd apps/bot && PYTHONPATH=src:tests ../../.venv/bin/python -m unittest discover -s tests
cd apps/bot && ../../.venv/bin/basedpyright --pythonpath ../../.venv/bin/python src
cd apps/emby-manager && PYTHONPATH=src ../../.venv/bin/python -m unittest discover -s tests
```

---

## P1：逻辑问题（必须修）

### 1. 编辑题卡失败后重发，会给成员重新计满答题时间

- **现状**：答错换题后，如果编辑题卡失败，`onboarding.py` 的 `verify_group_challenge` 调用 `retry_failed_group_challenge_edit`，把这道题放进重发队列。重发走 `_start_group_challenge`，它会把截止时间设成"现在 + 完整时长"（见 `_start_group_challenge` 里的 `set_group_challenge_message_id(... delivered_at + ttl ...)`）。
- **影响**：和文档里"换题保留原截止时间"的承诺不一致，成员多拿到一段答题时间。答题次数没有被重置。
- **期望**：已经答过题的验证（`verification_attempts > 0`），或者是从换题失败恢复过来的验证，重发时沿用原截止时间。如果原截止时间已经过去或太近（比如少于 30 秒），可以给一个最短作答窗口，但不能是完整时长。首次送达仍然从送达时刻开始计时。
- **测试**：模拟"答错一次 → 编辑失败 → 重发成功"，断言新的截止时间不晚于原截止时间（或等于你设定的最短窗口）。

### 2. 大模型返回的格式校验太严，复核卡会变多

- **现状**：`llm.py` 的 `_ModerationResponse` 下面几种情况都会让整条返回校验失败，交给人工复核：
  - `category` 为 null
  - `confidence` 是字符串 `"0.9"`
  - `evidence` 数组里有数字
- **影响**：新成员首次发言每条都会送去大模型审核，格式稍有偏差就多一张管理员复核卡。
- **期望**：
  - `is_ad` 继续严格校验，只接受真正的布尔值，绝不能把 `"false"` 当成真。
  - `confidence` 必须是 0–1 之间的有限数值。数字字符串是否允许请自己决定并写明理由；NaN 和越界值一律拒绝。
  - `category`、`reason` 为 null 或其他非字符串类型时，回退成默认值（`"other"` 和 `""`）。
  - `evidence` 不是数组时视为空；数组里不是字符串的项丢掉或转成字符串。
- **测试**：上面每种情况各写一个用例。另外要验证 `is_ad: "false"`、`is_ad: 1`、`confidence: 1.5` 仍然被拒绝。

---

## P2：小问题（建议修）

### 3. 审批和换题撞车时，题卡不会被更新

- 换题期间 `verification_message_id` 会暂时变成 NULL。如果管理员这时用新题卡上的按钮点了"通过"或"拒绝"，`resolve_group_challenge_by_admin` 能成功，但因为找不到 `message_id`，题卡不会被换成欢迎语或失败提示。要等 30 分钟后的兜底清理，这张题卡才会被删掉，期间群里一直挂着一道已经失效的题。
- **期望**：管理员处理时如果没有 `message_id`，就去用正在进行中的题卡 ID（比如 `scheduled_message_deletions` 里待清理的那条）更新题卡；或者换题流程在登记失败时检查状态，自己把题卡换成最终提示。选一种做法，补测试。

### 4. 重发后登记失败的结果被忽略

- `_start_group_challenge` 里 `_ = self.repository.set_group_challenge_message_id(...)` 没有检查返回值。题卡发出去了，但登记失败（状态被管理员改了、token 不匹配），这张题卡就会以按钮全部失效的状态留在群里。
- **期望**：返回 False 时删除或替换刚发出的题卡（失败只记日志），并记录一条 warning。

### 5. Emby 防误归档检查会连带中止 MoviePilot 同步

- `api.py` 的 `run_full_tracking_sync` 先调用 `manager.sync_series()`。防误归档检查抛出 `RuntimeError` 后，后面的 MoviePilot 订阅同步也不会执行。
- **期望**：把剧集同步的失败记进 `sync_status`，然后继续跑 MoviePilot 同步，最后再把剧集同步的错误报出来；或者明确写明为什么必须整体中止。

---

## P3：早就存在的性能问题（不是这次引入的，可以单独排期）

先评估再动手；改动较大的请先写方案，不要直接改。

- **PostgreSQL 整表锁**：`database.py` 的 `PostgresConnection.execute("BEGIN IMMEDIATE")` 会锁住整张 `member_onboarding` 表，但 `claim_webhook_update`、`claim_moderation_review` 这些根本不涉及该表的事务也在用它，结果每条 webhook 更新都在争同一把锁。建议改成按业务加锁：`SELECT ... FOR UPDATE`、条件 UPDATE、`INSERT ... ON CONFLICT`，或者 advisory lock。这里关系到题目作答只能被处理一次，改之前要列出每个调用点需要的保证，并补充真实 PG 下的并发测试。
- **没有连接池**：每次数据库调用都新建一个 psycopg 连接。可以考虑 `psycopg_pool`。
- **管理员缓存只增不减**：`AiogramGateway._admin_cache` 过期的条目从不清理，会随着发言人数一直变大。加上容量上限或定期清理即可。
- **审计接口每次请求都查 Telegram**：`audit_api._build_group_admin_checker` 每次都调 `getChatMember`，而且每次都新建 httpx 客户端。加一个短 TTL 缓存（例如 60 秒），并复用同一个客户端。

---

## 已确认不需要改

- webhook 密钥比较、Telegram 初始数据校验、审计接口的群范围校验、审批按钮带 chat_id 防止跨群使用、题卡 HTML 转义：都没问题。
- `ADMIN_USER_IDS` 中的运营者即使不是群管理员，也能在审计页执行封禁和恢复。README 写明这是有意设计，保留。

## 修复记录（由 Codex 填写）

<!-- 每条：改动文件、做法、新增测试、验证命令和结果；没修的写明原因 -->

### 验证汇总（2026-10-24，本地修改，未部署、未提交）

先运行了文件开头三条命令确认基线：Bot 发现 178 项，174 项执行通过、4 项 PG 测试跳过；basedpyright 为 0 errors / 0 warnings；Emby 4 项通过。

修复后按相同命令复跑：

| 编号 | 命令（在仓库根目录分别执行） | 结果 |
| --- | --- | --- |
| B1 | `cd apps/bot && PYTHONPATH=src:tests ../../.venv/bin/python -m unittest discover -s tests` | 发现 198 项：193 项执行通过，5 项 PG 集成测试跳过；退出码 0 |
| B2 | `cd apps/bot && ../../.venv/bin/basedpyright --pythonpath ../../.venv/bin/python src` | 0 errors / 0 warnings / 0 notes；退出码 0 |
| B3 | `cd apps/emby-manager && PYTHONPATH=src ../../.venv/bin/python -m unittest discover -s tests` | 9 项执行通过；退出码 0 |

`git diff --check` 通过。仍有原有的 Starlette 测试客户端弃用提示，不属于 Python 静态类型警告。PG 跳过数量从 4 变成 5，是因为新增了一个真实驱动回归用例；未配置 `MISAKABOT_TEST_DATABASE_URL`，本轮没有访问远程服务器或操作线上数据库，不能把它们计作已执行通过。

### P1-1：已修复——恢复换题沿用原截止时间

- **改动文件**：`apps/bot/src/misakabot/onboarding.py`、`repository.py`、`telegram_gateway.py`、`gateway.py`；测试位于 `apps/bot/tests/test_onboarding.py`、`test_gateway.py`。
- **做法**：增加有类型的 `GroupChallengeSnapshot`，读取当前 token、答题次数、原截止时间和题卡 ID。已答过题（`verification_attempts > 0`）的重发按 `max(原截止时间, 实际送达时间 + 30 秒)` 登记，不再重新给完整时长；换题失败路径一定已计入一次答错，因而也适用。首次题卡送达仍从送达时刻计完整时长。重发题卡显示绝对截止时间，不再声称重新获得两分钟；日志同时记录实际截止时间及是否为答题恢复。
- **新增/增强测试**：增强 `test_failed_rotation_recovers_without_resetting_attempts` 验证“答错→编辑失败→过期后重发”只给 30 秒；新增 `test_recovered_rotation_preserves_a_still_usable_original_deadline`、`test_recovery_near_original_deadline_only_grants_minimum_window`、`test_first_delivery_still_gets_full_time_after_send_succeeds`、`test_recovered_send_displays_original_absolute_deadline`，分别覆盖仍有充足时间、仅剩 20 秒、首次发送有延迟及题卡文案。
- **验证结果**：B1、B2 均通过，原有次数保留、防重放、超时及未送达恢复测试未退化。未改数据库表结构和锁策略。

### P1-2：已修复——只严格校验会改变裁决的字段

- **改动文件**：`apps/bot/src/misakabot/llm.py`、`apps/bot/tests/test_llm.py`。
- **做法**：`is_ad` 保持 `StrictBool`。允许可解析的数字字符串置信度（含首尾空白、指数形式），原因是它们仍表达相同的数值，无需因此增加人工卡片；转换后仍必须是 0～1 的有限数字，拒绝布尔值、NaN、Infinity、越界及非数字字符串。`category`、`reason` 为非字符串时分别回退 `other`、空字符串。`evidence` 不是数组时置空，数组中只保留字符串项，不将任意对象格式化成“证据”。
- **新增测试**：`test_null_or_non_string_category_falls_back_without_losing_verdict`、`test_null_or_non_string_reason_falls_back`、`test_numeric_string_confidence_is_accepted`、`test_non_array_evidence_is_treated_as_empty`、`test_non_string_evidence_items_are_dropped`。更新原格式拒绝用例中已按要求放宽的 evidence 场景，同时继续并增加断言 `is_ad: "false"`、`is_ad: 1`、`confidence: 1.5`、负数、字符串 NaN/Infinity 等被拒绝。
- **验证结果**：B1、B2 均通过；原合法布尔值、数值边界及请求重试用例继续通过。

### P2-3：已修复——管理员处理换题中的题卡

- **改动文件**：`apps/bot/src/misakabot/handlers/onboarding.py`、`onboarding.py`、`repository.py`；测试位于 `test_onboarding.py`、`test_handler_routing.py`、`test_postgres_integration.py`。
- **做法**：选择使用 Telegram 回调自带的精确题卡 ID，而不是从同群清理队列猜测属于哪个成员的消息。处理器传递实际 `chat_id` 和 `callback_message_id`；在已有 token/用户状态/群范围校验之后，仓储仅在原消息 ID 为 NULL 时认领该 ID，并与管理员状态变更放在同一事务。已有登记 ID 不会被回调覆盖。若迟到换题编辑覆盖了管理员的结果，登记失败分支读取最终状态并再替换一次欢迎语/失败提示。尚在执行 Telegram 操作的管理员已认领题卡时，不抢先删卡或误报通过；操作失败仍可按原流程恢复待验证。
- **新增测试**：`test_admin_decision_during_rotation_updates_the_clicked_card`（通过、拒绝两个分支及迟到编辑）、`test_admin_callback_fallback_cannot_override_registered_message`、`test_rotation_does_not_delete_a_card_adopted_by_an_inflight_admin`、`test_admin_final_card_survives_rotation_api_error`、`test_admin_callback_passes_its_bound_chat_and_message_id`。新增可选 PG 用例 `test_admin_adopts_inflight_message_with_typed_snapshot`，覆盖 PG 时间值转换、错误群拒绝及正确消息认领。
- **验证结果**：B1、B2 均通过，已有跨群 token 拒绝测试继续通过。新增 PG 用例因本地未提供测试 DSN 跳过，尚未实测真实 PG；未为此部署或连接生产数据库。

### P2-4：已修复——检查发送/换题登记结果并回收失效卡

- **改动文件**：`apps/bot/src/misakabot/onboarding.py`、`apps/bot/tests/test_onboarding.py`。
- **做法**：首次发送、重发及原地换题都检查登记返回值，失败记录 `onboarding.group_challenge_registration_rejected` warning。失去所有权的独立新卡会删除；管理员已完成处理时替换为最终提示；管理员已认领、仍在处理的同一张卡留给管理员流程收尾。失败发送不再返回可用 token，也不计入成功重发数。清理/替换失败只记录日志，保留或重新安排一分钟后的持久化清理；替换最终提示成功时取消旧题卡兜底，避免欢迎语被旧任务误删。
- **新增测试**：`test_failed_delivery_registration_deletes_unregistered_card_and_returns_no_token`、`test_failed_registration_cleanup_error_only_logs_and_keeps_durable_guard`、`test_failed_final_replacement_after_late_edit_rearms_cleanup`、`test_failed_registration_after_admin_approval_replaces_newly_sent_card`、`test_recovery_with_rejected_registration_is_not_counted_as_sent`；另由 P2-3 的并发用例覆盖换题登记失败后的收尾。
- **验证结果**：B1、B2 均通过，清理异常不向成员操作调用方抛出，未改变封禁/放行裁决。

### P2-5：已修复——剧集防误归档失败不阻断 MoviePilot

- **改动文件**：`apps/emby-manager/src/emby_manager/api.py`、新增 `apps/emby-manager/tests/test_full_tracking_sync.py`。
- **做法**：完整追更同步按阶段捕获异常，剧集失败时在 `sync_status.series` 中记录 `last_error`、`last_attempt_at`，保留最近一次真正成功的结果和时间，再继续 MoviePilot。MoviePilot 的成功或失败独立记录。最后汇总并抛出异常，接口返回 502，不把部分成功伪装成全量成功。依赖 Emby 剧集快照的 TMDB 追更阶段在剧集失败时跳过并记录原因，避免基于防误归档检查已拒绝的快照做后续判断；剧集成功而 MoviePilot 失败则仍可执行 TMDB。取消异常不吞掉、不继续同步。
- **新增测试**：`test_archive_guard_failure_does_not_skip_moviepilot`、`test_success_after_failure_clears_errors_and_releases_sync_lock`、`test_both_failures_are_recorded_without_reporting_success`、`test_moviepilot_failure_does_not_skip_independent_tracking_after_series_success`、`test_cancellation_is_not_swallowed_or_continued`。通过真实 ASGI 路由、测试会话/CSRF 和模拟服务执行，不连接真实 Emby、MoviePilot 或数据库。
- **验证结果**：B3 的 9 项全部通过（原有 4 项未退化），包括状态保留、成功后清错及下一次请求可以继续执行。

### P3：仅评估与方案，未修改代码，等待确认

以下四项本轮均未实现；原因是用户要求先评估，且事务/连接生命周期/授权缓存涉及独立的行为与并发风险。`database.py`、`audit_api.py` 的相关实现及 Gateway 管理员缓存保持原样。

#### A. PostgreSQL 整表锁：风险最高，建议单独一批改

当前有五处 `BEGIN IMMEDIATE` 调用，不能直接将其统一替换为普通 `BEGIN`：

| 调用点（`repository.py`） | 必须保留的保证 | 拟采用的 PG 方案 |
| --- | --- | --- |
| `claim_webhook_update` | 同一 update_id 只能有一个活动租约；已完成不再执行；过期租约只能被一个请求接管 | 用主键保护 `INSERT ... ON CONFLICT DO NOTHING RETURNING`；已有行使用带状态/租期条件的 `UPDATE ... RETURNING`。时间比较放在数据库中，不依赖 PG datetime 的字符串格式 |
| `claim_moderation_review` | 同一群同一事件只允许一个管理员获得 pending→resolving 的处理权，读取目标和领取不能分离 | 保留条件 UPDATE，并将目标字段直接 RETURNING；失败时回滚，不再锁 member_onboarding |
| `resolve_group_challenge` | token、用户、pending 状态、送达 ID、期限同时有效；一次点击只计一次；换题、次数与旧卡兜底原子更新 | 对目标 member_onboarding 行 `SELECT ... FOR UPDATE` 后重新判定条件，或等价条件 UPDATE RETURNING；同一事务写清理任务 |
| `resolve_group_challenge_by_admin` | 群范围校验；和用户答题/超时竞争只胜出一个；消息认领与状态变更原子化 | 对同一成员行使用与用户答题一致的行锁顺序；条件更新后返回目标；不同群/成员不互相阻塞 |
| `consume_verification` | 一次性 token 只能消费一次，且绑定用户/状态/期限；不能覆盖管理员已处理状态 | 单条带完整条件的 UPDATE RETURNING，或对目标行加锁后消费 |

实施时新增显式事务入口，不再靠通用 execute 中的字符串替换决定 PG 锁策略；SQLite 保留其现有写事务语义。`claim_expired_verification`、消息清理租约、手动批准等虽不是上述五个入口，也必须一起审查条件 UPDATE 和锁顺序，保证不会绕过同一成员的并发协议。只读 snapshot 不能代替原子领取或送达登记 CAS。

**确认后必须补的测试**：在隔离 PG 测试数据库使用多个独立连接并发提交同 token 两次、正确/错误同时点击、答题与管理员处理、处理与超时、换题与消息登记、重复 webhook/过期租约抢占、同事件双管理员；断言计数/领取只有一次、跨群仍拒绝。同时证明不同成员、不同群、webhook 与审核事件不再争同一表锁。并发协调用屏障/事件，不以 sleep 猜测顺序；设置 lock_timeout / statement_timeout 并检查所有连接正确回滚和释放。未经这组真实 PG 测试，不替换现有锁。

#### B. 连接池：收益明确，放在事务改造后单独实施

- 当前每个仓储调用都建立 psycopg 连接，Bot、audit-api 各自进程重复支付连接成本；新 snapshot 查询也会产生一次连接。先测连接数、连接耗时和仓储调用频率，再决定容量。
- 拟增加 `psycopg_pool`，每进程一个有限池（候选 min_size=1、max_size=4～8、领取超时 5 秒，最终按数据库连接预算确认）；不能按每次调用重新建池。
- 由进程/应用 lifespan 显式 open、就绪检查和 close；适配器 close 归还租约而不是关闭物理连接。归还前必须 rollback 未提交或失败事务，保留 tuple row_factory、JSONB 和时间类型，禁止不同请求共用一个活动事务。
- 验证池上限与等待超时、异常连接淘汰/重连、取消归还、启动失败和正常退出、事务失败后下一请求不受污染；临时表集成测试必须明确借用同一连接，不可依赖池随机分配会话。暂不同时改异步驱动，避免扩大范围。

#### C. Gateway 管理员缓存：改动较小，可优先单独实施

- `_admin_cache` 当前键是 `(chat_id, user_id)`，实际也缓存大量普通成员；TTL 失效只影响命中，不回收内存，因此确会增长。
- 方案：保留现有 30 秒 TTL 与成员角色事件的立即 invalidate；新增有界 LRU（候选容量 4096）和插入时的过期清理，容量满先淘汰过期项，再淘汰最久未使用项。不缓存 Telegram 异常，不将查询失败当作管理员。
- 测试至少覆盖万级不同用户容量有界、过期项实际删除、LRU 顺序、跨群隔离、角色变更立即失效、失败后可重试；用可控 monotonic 时钟，不等待真实时间。不会改变普通成员命令的静默处理策略。

#### D. 审计 Telegram 查询缓存与客户端复用：注意撤权时效

- 当前每次非运营者请求都创建 httpx 客户端并调用 getChatMember；有连接复用及短 TTL 缓存的优化空间，但缓存管理员权限会引入撤权延迟。
- 方案：由 audit-api lifespan 管理共享 AsyncClient；缓存键为 `(chat_id, user_id)`，设置容量上限与最多 60 秒 TTL。只读列表/详情允许短缓存；封禁、恢复等写操作重新向 Telegram 核验权限，避免用旧缓存执行高影响动作。并发相同键可合并查询，但不能吞取消异常或缓存失败。
- Telegram initData 签名、时效及群范围依然每次验证；不缓存身份校验结果。`ADMIN_USER_IDS` 的运营者例外保持现有有意设计，不能借优化收紧或扩大权限。
- 测试共享客户端只创建一次并在退出关闭、重复请求减少外呼、过期刷新、跨群隔离、权限撤销后写操作立即拒绝、HTTP/格式错误 fail-closed、不产生无界缓存，以及高并发同键查询合并。测试用 MockTransport，不触碰真实群成员。

建议后续顺序：先确认缓存容量/授权时效方案，再分别做缓存；PG 行级事务改造和连接池分别提交、分别验证，不与本轮 P1/P2 混做。

---

## 第二轮：复核后剩余问题（2026-10-24）

第一轮 P1/P2 的 5 项修复已复核，做法正确。复跑结果与上面的记录一致：Bot 193 项通过、5 项 PG 跳过；basedpyright 0/0；Emby 9 项通过。下面是剩余问题，修完请在本节末尾补记录。P3 方案已认可，但本轮仍不实施。

### R1（中）：Bot 自身失败后，成员只剩 30 秒作答

- **现状**：`_start_group_challenge` 恢复已答过的题时，截止时间取 `max(原截止时间, 送达时间 + RECOVERED_CHALLENGE_MINIMUM_SECONDS)`，其中最短窗口是 30 秒。题卡编辑或发送失败是 Bot 一侧的问题，成员此时看到的提示是"题目更新失败，正在重新发送，请稍候"。如果重发晚于原截止时间（重试至少要等 2 分钟），成员只剩 30 秒作答，很容易因为 Bot 的故障超时被移出。
- **期望**：
  - 编辑失败时把当时剩余的作答时间保存下来，重发送达后按这段剩余时间重新计时：新截止时间 = 送达时间 + 失败时剩余的时间，再与最短窗口取较大值。
  - 最短窗口提高到不少于 60 秒，建议 90 秒。
  - 剩余时长的存法由你决定，但不要把它塞进 `verification_expires_at` 这类语义不同的列里。如果确实需要新增列，请同时补齐 SQLite 和 PostgreSQL 两边的迁移，并在记录里写明理由。
  - 不能借此重置答题次数，也不能让成员通过任何操作延长时间。重发能触发的前提必须仍然只有 Bot 自身失败。
  - 首次送达照旧给完整时长，行为不变。
- **测试**：
  - 失败时还剩 80 秒，重发晚了 5 分钟才送达：送达后应该还有约 80 秒。
  - 失败时只剩 5 秒：送达后按最短窗口计时。
  - 首次送达的行为不变。
  - 恢复后答题次数保持不变。
  - 管理员已经处理过的验证，不会被恢复流程重新打开。

### R2（小）：业务代码里直接写 SQL

- **现状**：`onboarding.py` 中 `_handle_unregistered_group_challenge` 和 `_replace_group_challenge_message` 通过 `self.repository.execute_write("DELETE FROM scheduled_message_deletions ...")` 直接执行 SQL，绕过了数据层（repository）的封装。另外，每次把题卡换成最终提示时都要多建一次数据库连接。
- **期望**：在 `AuditRepository` 里新增 `cancel_message_deletion(chat_id, message_id) -> bool`，两处调用改用这个方法，`OnboardingService` 里不再出现 SQL 字符串。如果能和已有的数据库写操作合并到同一次连接里，就顺便合并；做不到也没关系，不必为此大改。
- **测试**：给 `cancel_message_deletion` 写一个仓储层单测，覆盖存在和不存在两种情况。原有清理相关的测试不能退化。

### R3（小）：题卡显示的截止时间与实际登记的截止时间不一致

- **现状**：恢复路径里，传给 `send_group_vps_challenge` 的显示截止时间在发送前计算，登记的 `answer_deadline` 在发送后按 `delivered_at` 计算。原截止时间已过时，两者会相差 Telegram 请求的耗时。
- **期望**：两者使用同一个值。可以在发送前确定截止时间，登记时直接沿用；也可以在登记时取两者中较晚的一个，并保证显示给成员的时间不早于实际截止时间。哪种做法都行，但成员看到的截止时间不能早于真实截止时间。修 R1 时一起处理即可。
- **测试**：用一个慢速的假 gateway（发送前后时钟前进若干秒），断言显示的截止时间不早于登记的截止时间。

### 上线前必须做（不是代码修改）

- 新增的 `test_admin_adopts_inflight_message_with_typed_snapshot` 以及其余 PG 集成测试，要在设置 `MISAKABOT_TEST_DATABASE_URL` 的独立测试库上跑通，并在记录里写明结果。只能用测试库或临时表，不能连接生产库。没有可用的测试库时，请如实写明"未执行"。

### 第二轮修复记录（由 Codex 填写）

<!-- 每条：改动文件、做法、新增测试、验证命令和结果；没修的写明原因 -->

#### 验证汇总（2026-10-24，本地修改，未部署、未提交）

本轮开始先运行文件开头三条命令：Bot 发现 198 项，193 项执行通过、5 项 PG 集成测试跳过；basedpyright 0 errors / 0 warnings；Emby 9 项通过。修复完成后使用相同命令验证：

| 命令（在仓库根目录分别执行） | 最终结果 |
| --- | --- |
| `cd apps/bot && PYTHONPATH=src:tests ../../.venv/bin/python -m unittest discover -s tests` | 发现 212 项：206 项执行通过，6 项 PG 集成测试跳过；退出码 0 |
| `cd apps/bot && ../../.venv/bin/basedpyright --pythonpath ../../.venv/bin/python src` | 0 errors / 0 warnings / 0 notes；退出码 0 |
| `cd apps/emby-manager && PYTHONPATH=src ../../.venv/bin/python -m unittest discover -s tests` | 9 项执行通过；退出码 0 |

`git diff --check` 通过，测试和静态类型检查均未退化。原有 Starlette 测试客户端弃用提示仍存在。整秒显示修正后的首次全量复跑曾出现 3 条仍比较微秒的测试断言失败；将这三条恢复测试的模拟送达时刻固定为整秒，另补专门的微秒边界测试后，全量复跑通过，未放宽截止时间与显示时间必须一致的断言。

#### R1：已修复——保存 Bot 故障时的剩余作答时间

- **改动文件**：`apps/bot/src/misakabot/repository.py`、`onboarding.py`、`gateway.py`、`telegram_gateway.py`；测试为 `apps/bot/tests/test_recovery_budget.py`、`test_repository_recovery.py`、`test_onboarding.py`、`test_postgres_repository.py`、`test_postgres_integration.py`。
- **做法**：新增可空整数列 `member_onboarding.verification_remaining_seconds`，只保存 Bot 编辑失败时冻结的剩余秒数。理由是暂停期间仍要保留 `verification_expires_at` 作为投递失败最终退出的时间锚点，不能让绝对截止时间兼任剩余时长。首次失败在同一事务中撤销旧 token、保存剩余时间并安排旧卡清理；重复投递失败保留已冻结的时间。成功恢复后按“送达时刻 + max(冻结秒数, 60)”设置截止时间并清空暂停值，保留已使用次数。采用 **60 秒**最短窗口：满足不少于 60 秒的要求，也让剩余 80 秒的成员恢复后仍取得约 80 秒，而不是被统一调整为 90 秒。
- **升级方式**：SQLite 新表包含该列，旧表通过 `PRAGMA table_info` 判断并执行 `ALTER TABLE ... ADD COLUMN`；PostgreSQL 新表包含该列，旧表在原有初始化 advisory lock 内执行 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`。两边均为可重复执行的加列升级，不重建表、不删除数据、不重置答题次数。旧版本或进程中断遗留的未送达换题记录没有暂停值时，按原截止时间减去最后一次持久化编辑时间计算，随后持久化冻结值；领取还校验原 `updated_at`，避免覆盖竞争领取。
- **边界保证**：正常答错、重复回调和重复入群事件均不增加作答时间；恢复仅处理 Bot 未送达/中断的题卡。新的首次送达仍取得完整配置时长。恢复登记保留 token/待验证状态条件，管理员已处理的记录不能被恢复流程重新打开。保留原有 30 分钟未送达兜底，反复 Bot 故障不会无限延后其退出时间。本条取代第一轮 P1-1 中“原截止时间或 30 秒窗口”的恢复策略。
- **新增/增强测试**：`test_recovery_budget.py` 覆盖剩余 80 秒且中断 5 分钟、剩余 5 秒取得 60 秒窗口、次数不变、重复点击/答错/入群不能续时、连续 Bot 故障保留同一预算、管理员通过/拒绝竞争、进程中断恢复和过期 token 的登记拒绝；`test_onboarding.py` 保留并更新恢复测试，首次慢速发送仍给完整时长的测试继续通过。`test_repository_recovery.py` 用真实缺列的旧 SQLite 表验证加列后两次初始化均不重置次数、截止时间或已保存预算。`test_postgres_repository.py` 验证建表和幂等 ALTER 均在初始化锁后执行（模拟连接，不算真实 PG 实测）。新增可选真实驱动测试 `test_paused_recovery_budget_native_timestamps_and_deletion_cancel`，覆盖 PG 原生时间值、冻结/恢复、陈旧 token 和清理取消。
- **验证结果**：Bot 全量测试及类型检查通过；新增 PG 集成用例尚未执行，见下方独立测试库记录。

#### R2：已修复——清理取消操作收回仓储层

- **改动文件**：`apps/bot/src/misakabot/repository.py`、`onboarding.py`；测试为 `apps/bot/tests/test_repository_recovery.py`，可选 PG 覆盖在 `test_postgres_integration.py`。
- **做法**：新增 `cancel_message_deletion(chat_id, message_id) -> bool`，存在并删除一条时返回 True，不存在时返回 False。`_handle_unregistered_group_challenge`、`_replace_group_challenge_message` 两处改调该方法，`OnboardingService` 中不再含 SQL。成功登记消息时的取消仍与登记同一事务提交；最终提示替换后的取消须等待 Telegram 成功，不能提前取消兜底，因此保留独立的短数据库写操作，没有为了省一个连接重排外部操作或扩大事务。
- **新增测试**：`test_cancel_deletion_existing_missing_and_group_isolation` 覆盖存在、不存在、重复取消及不同群/不同消息 ID 的隔离。原有定时删除、扫描竞争、租约重试、替换失败保留兜底和成功替换取消兜底等测试全部保留。
- **验证结果**：仓储单测、原有清理测试、Bot 全量测试和类型检查通过；PG 可选用例未执行。

#### R3：已修复——恢复题卡和数据库共用一个截止时间

- **改动文件**：`apps/bot/src/misakabot/onboarding.py`、`gateway.py`、`telegram_gateway.py`；测试为 `apps/bot/tests/test_recovery_budget.py`、`test_gateway.py`。
- **做法**：恢复时先发送无作答/审批按钮的准备提示，保留查看资料入口。收到 Telegram 返回的消息 ID 后，计算唯一的截止时间，再用该值编辑正式题卡并登记数据库；登记失败沿用现有失效卡清理/最终提示收尾。恢复题卡显示到整秒，因此计算结果只向上对齐一次到整秒（不足一秒的对齐），题卡显示与数据库完全一致，不会显示一个更早的截止时间，也不缩短 60 秒最短窗口。首次送达逻辑不变。
- **新增测试**：`test_slow_send_uses_one_visible_and_registered_deadline` 用发送耗时 25 秒的假 gateway 验证送达后仍有 80 秒且显示/存储相等；`test_fractional_delivery_rounds_up_once_to_the_visible_second` 覆盖带微秒的送达时刻；`test_recovery_placeholder_has_no_answers_or_deadline_until_final_edit` 验证准备提示没有失效答案、审批或截止时间，正式题卡展示精确的 UTC+8 截止时间。原有题卡 HTML/绝对时间渲染测试继续通过。
- **验证结果**：上述专项测试、Bot 全量测试和类型检查通过。

#### 独立 PostgreSQL 集成测试：未执行

本轮未配置 `MISAKABOT_TEST_DATABASE_URL`，本机未发现可直接使用的 PostgreSQL 服务端工具，Docker 服务也无法通过当前权限访问；未获得可用的独立测试库，未设置生产 DSN，未连接生产库。6 项 PG 集成测试（包括 `test_admin_adopts_inflight_message_with_typed_snapshot` 和本轮新增用例）均为跳过，不能计为通过。SQLite 升级实测和 PG 模拟连接测试不能代替真实 PG 集成测试；上线前仍须在独立测试库设置该变量后执行 Bot 测试命令并确认这些测试通过。

#### P3 及交付边界

P3 仍未实施，上一轮评估与方案保留；本轮没有修改整表锁策略、连接池、管理员缓存或审计权限缓存。未部署、未提交，也未推送；前一轮尚未提交的修改完整保留。

---

## 第三轮：退群重进会重置答题次数（2026-10-24）

R1–R3 已复核通过，复跑结果与第二轮记录一致：Bot 206 项通过、6 项 PG 跳过；basedpyright 0/0；Emby 9 项通过。本轮只处理 R4，以及顺带的 R5。P3 仍然不实施。

### R4（高）：二次验证未完成时退群再进，答题次数会被重置

- **复现**（已在本地用 SQLite 和测试里的 `FakeGateway` 实测）：
  1. `start_direct_join` 让成员入群并收到题卡；
  2. 连续答错 2 次，提示"还可尝试 1 次"；
  3. 触发 `mark_member_departed`，模拟成员退群；
  4. 再次 `start_direct_join`：成员拿到一道全新的题和完整的 3 次机会，期间没有任何封禁。
- **原因**：`repository.mark_member_departed` 会把 `SECONDARY_VERIFICATION_PENDING` 状态改成 `DECLINED`，同时清空 token、截止时间和 `verification_attempts`。之后 `start_direct_join` 遇到 `DECLINED` 状态，就按新成员重新发题。
- **影响**：脚本可以不停地"答错 → 退群 → 重进"，直到猜对。"答错就换题"和"三次机会"的防护因此基本失效。直接入群的群组受影响最大；需要审批入群的群组，每次重进都要重新通过 Turnstile，成本高一些，但同样能反复刷。
- **期望**：
  - 二次验证还没完成（`SECONDARY_VERIFICATION_PENDING`，包括题卡暂停或重发中的状态）时退群，按验证失败处理，执行 `_ban_failed_verification`，封禁时长遵守 `SECONDARY_VERIFICATION_BAN_HOURS`（0 表示永久）。封禁到期后重新申请入群，必须重新完成全部验证。
  - 退群时群里如果还挂着这个人的题卡，换成失败提示，或者按现有清理机制删除，不能留下可以点的按钮。
  - 已经通过验证的成员（`PENDING_FIRST_MESSAGE`、`OBSERVING`）退群，行为保持不变。
  - 处理退群必须是原子操作，并且和答题、管理员处理、超时处理互斥：管理员已经通过的成员不能因为一个迟到的退群事件被封禁；超时已经封禁过的，也不能重复封禁。沿用现有的条件 UPDATE 认领方式（参考 `claim_expired_verification`），在调用 Telegram 封禁之前先认领。
  - 如果 Telegram 封禁失败，要能重试（比如恢复成可被超时清理接手的状态），不能变成"既没封禁、次数又清零"。
  - **被管理员踢出**和**成员主动退出**要分清。Telegram 的 `chat_member` 更新会区分 `left` 和 `kicked`；被管理员或 Bot 踢出的情况不要再额外封禁一次，如果需要区分，请在记录里写明你的判断依据。
  - 如果判断"一律封禁"会误伤正常用户（比如成员不小心退群），可以退一步：至少把已答错次数和剩余作答时间保留下来，重进后接着算，不能重置。请选一种做法，并写明理由。
- **测试**：
  - 上面的复现场景：退群后再进，不能拿到新的 3 次机会。按你选的做法，断言成员被封禁，或者次数延续。
  - 已通过验证的成员退群，行为不变。
  - 管理员先通过、退群事件后到：不封禁。
  - 超时先封禁、退群事件后到：不重复封禁，也不报错。
  - Telegram 封禁失败后会重试，且答题次数没有被清零。
  - 题卡暂停或重发中退群：同样按失败处理，旧题卡和占位卡都会被清理。
  - 被踢出和主动退出两种情况分别覆盖。
  - 需要审批入群的流程（`join_request`）：二次验证中退群后重新申请，同样不能绕过。

### R5（小）：最大答题次数在两处写死

- `onboarding.py` 里恢复题卡时用 `max(0, 3 - snapshot.attempts)`，`repository.resolve_group_challenge_by_admin` 里也有 `3 - attempts`，和 `resolve_group_challenge` 的 `max_attempts` 默认值重复定义。
- **期望**：抽成一个常量（例如 `OnboardingService.MAX_GROUP_CHALLENGE_ATTEMPTS`，或者放进 repository 共用），所有用到的地方都引用它，不再出现裸数字 3。行为不变。

### 上线前仍需完成

PG 集成测试（加上本轮新增的用例）要在独立的测试库上跑通，规则同前：只能用测试库或临时表，不能连接生产库；没有测试库就如实写"未执行"。

### 第三轮修复记录（由 Codex 填写）

<!-- 每条：改动文件、做法与理由、新增测试、验证命令和结果；没修的写明原因 -->

#### 验证汇总（2026-10-24，本地修改，未部署、未提交）

本轮修改前运行开头三条命令确认基线：Bot 发现 212 项，206 项执行通过、6 项 PG 集成测试跳过；basedpyright 0 errors / 0 warnings；Emby 9 项通过。修复后按相同命令验证：

| 命令（在仓库根目录分别执行） | 最终结果 |
| --- | --- |
| `cd apps/bot && PYTHONPATH=src:tests ../../.venv/bin/python -m unittest discover -s tests` | 发现 231 项：224 项执行通过，7 项 PG 集成测试跳过；退出码 0 |
| `cd apps/bot && ../../.venv/bin/basedpyright --pythonpath ../../.venv/bin/python src` | 0 errors / 0 warnings / 0 notes；退出码 0 |
| `cd apps/emby-manager && PYTHONPATH=src ../../.venv/bin/python -m unittest discover -s tests` | 9 项执行通过；退出码 0 |

`git diff --check` 通过。原有 Starlette 测试客户端弃用提示仍存在，未新增未等待协程等运行时警告。测试和类型检查均未退化。

#### R4：已修复——未完成二次验证时主动退出，按验证失败封禁

- **选择与理由**：选择“按验证失败封禁”，不采用退群后继续答题。主动退出期间成员看不到群内题卡，继续保留/暂停作答会增加反复切换入群阶段的复杂性；统一失败并按原配置冷却，更直接地阻断“答错两次→退出→重新获取三次”的绕过。封禁仍调用 `_ban_failed_verification`，遵守 `SECONDARY_VERIFICATION_BAN_HOURS`，默认 1 小时、0 表示永久。封禁到期后重新申请必须重新走正常验证流程。
- **改动文件**：`apps/bot/src/misakabot/domain.py`、`repository.py`、`onboarding.py`、`gateway.py`、`handlers/onboarding.py`；`handlers/admin.py`、`audit_api.py` 仅衔接现有解封/恢复入口与新增的群内冷却记录。测试为新增 `apps/bot/tests/test_verification_departure.py`，以及 `test_handler_routing.py`、`test_onboarding.py`、`test_repository_recovery.py`、`test_postgres_repository.py`、`test_postgres_integration.py`、`test_gateway.py`。
- **原子认领与互斥**：`claim_verification_departure` 先以条件 UPDATE 将 `SECONDARY_VERIFICATION_PENDING` 认领为 `VERIFICATION_DEPARTURE_PENDING` 并撤销 token，事务提交后才调用 Telegram。认领同时保留已答错次数、截止时间和冻结预算，并登记清理任务。原有答题/管理员事务与该写操作按数据库写锁序列化；超时也使用待验证状态条件认领，所以只能有一个处理方取得同一验证的处理权。已通过/已被超时处理的记录不满足退群认领条件，不会再被封禁。正常成员退群的清理只作用于已完成验证且没有活动处理 token 的 `PENDING_FIRST_MESSAGE` / `OBSERVING`，不会覆盖仍在进行的管理员放行。
- **失败与重试**：封禁失败不退回可重新申请的状态，保留 `VERIFICATION_DEPARTURE_PENDING` 和答错次数。`updated_at` 作为两分钟处理租约；维护任务对到期记录再次条件认领，重试 Telegram 封禁，进程重启也能继续。单次封禁调用有 60 秒超时，短于租约；完成登记使用认领时间 CAS，陈旧处理方不能覆盖新处理方。重试与普通超时共享原有 8 个并发名额，每轮最多读取 100 条退群重试记录，避免增加串行重试阻塞普通超时的情况。
- **防止重新入群重置**：成功封禁转为 `VERIFICATION_DEPARTURE_BANNED`，保存当前群的封禁截止时间。封禁待处理、冷却未结束或永久封禁期间，直接入群和申请入群均不发新题、不重置次数。两个创建验证的 UPSERT 也加入相同状态/冷却条件，不能只依赖业务层的事先查询。额外封住原 `start()` 中“查询到不在群内→restart_existing=True”覆盖仍在二次验证记录的路径，即使退群状态更新尚未到达，申请入群也不能重置次数。封禁到期后申请重新走第一阶段，再进入全新的二次验证；现有 `/unban` 和审计恢复成功后同步清除该群已完成的退群封禁冷却，保留管理员明确解封的作用，不解除其他群的冷却。
- **被踢出与主动退出的区别**：路由显式传递 `kicked=True/False`，判断依据是 Telegram 的新成员状态 `kicked`；`left` 以及 `restricted` 的 `is_member` 从 True 变 False 仍按退出处理。`kicked` 仅终止尚待完成的题卡，不再调用封禁；Bot 自己封禁发出的 `kicked` 更新若遇到已认领/已完成状态，也不会再次处理。已完成验证的成员被踢出保持原行为。
- **题卡清理**：清理队列新增可空 `verification_user_id`，题卡发送、答错轮换与编辑失败的队列记录均带成员归属。认领退群时将此成员的活动题卡和未收尾的轮换/占位卡纳入 60 秒清理，避免按群内队列猜测消息归属。已登记题卡即时替换为失败或终止提示，失败提示仍在一分钟后删除；暂停中的旧题卡按持久化任务回收。发送中的占位卡即使在退群之后才返回，也会通过发送后的状态复查或登记失败收尾替换/清理，不能重新激活题目。Telegram 编辑失败仍保留删除兜底。
- **数据库升级与理由**：新增 `member_onboarding.verification_ban_until`（SQLite TEXT / PG TIMESTAMPTZ），与作答截止时间区分，确保本地不会在封禁未完成或冷却期间重置验证。新增 `scheduled_message_deletions.verification_user_id`（SQLite INTEGER / PG BIGINT），用于精确关联题卡清理。两列均同时加入新表结构与旧表加列迁移：SQLite 先检查 PRAGMA 再 ALTER，PG 在原有初始化 advisory lock 内 `ADD COLUMN IF NOT EXISTS`；升级可重复执行，不重建表、不重置现有次数或删除已有清理任务。未修改 P3 的事务锁策略。

**R4 场景与测试对应：**

| 要求 | 新增/增强测试及结果 |
| --- | --- |
| 答错两次后退出、重进不能取得新的三次 | `test_wrong_twice_leave_and_rejoin_cannot_get_new_attempts`：封禁 1 小时、次数仍为 2、不发新题，通过 |
| 已通过 / 已进入观察期的成员退出保持原行为 | `test_completed_and_observing_departures_keep_existing_behavior`；原 `test_departure_retires_old_trusted_state_before_rejoin` 改为实际等待异步退群处理，通过 |
| 管理员先通过、迟到退出不封禁 | `test_admin_claim_before_late_departure_is_not_banned` 用事件屏障停在 Telegram 放行中，确保退出不覆盖认领；已完成通过场景亦覆盖，通过 |
| 正确答案先取得处理权，不被退出误封 | `test_correct_answer_claim_before_departure_is_not_banned`，通过 |
| 超时先封禁、迟到退出不重复 | `test_timeout_wins_then_late_departure_does_not_repeat_ban`，通过 |
| 退出先认领后，答题、管理员、超时与重复退出均不能再处理 | `test_departure_claim_wins_over_answer_admin_timeout_and_duplicate_leave` 用事件屏障停在 Telegram 封禁中，通过 |
| Telegram 封禁失败后重试，次数不清零 | `test_failed_ban_is_durable_retry_and_cannot_reset_attempts`：两个入群入口均不能重置，租约到期维护重试一次成功，通过 |
| 暂停和恢复发送中退出，清理旧卡与占位卡 | `test_departure_while_paused_without_recovery_does_not_restart_timer`、`test_paused_and_inflight_recovery_cards_are_retired`；增强 Gateway 测试验证队列确实写入成员归属，通过 |
| 被踢出不额外封禁、状态分开路由 | `test_kicked_pending_member_only_retires_card_without_extra_ban`、`test_kicked_status_is_forwarded_separately_from_voluntary_departure`；原 left/restricted 路由测试继续通过 |
| join_request 同样不能绕过，冷却后重新完成全部验证 | `test_join_request_reentry_requires_cooldown_then_all_stages`、`test_join_request_cannot_reset_pending_challenge_even_without_departure_update`，通过 |
| 条件创建不能覆盖已认领退出 | `test_rejected_creation_cannot_overwrite_departure_claim`：直接验证与 restart_existing 首次验证均拒绝，通过 |
| 配置时长、永久封禁和明确解封 | `test_configured_ban_hours_are_used_by_telegram_and_local_cooldown`、`test_zero_duration_is_permanent_and_explicit_unban_is_scoped`，通过 |
| 升级保持数据、PG DDL 同步 | `test_departure_columns_migrate_without_removing_existing_deletion_jobs` 实测 SQLite 旧表升级及重复初始化；增强 `test_recovery_schema_and_upgrade_are_both_applied_under_initialization_lock` 检查 PG 建表与 ALTER（模拟连接），通过 |

- **可选 PG 用例**：新增 `test_departure_claim_retry_cooldown_and_join_request_guards`，覆盖原生时间值、退群/管理员/超时的条件互斥、重试认领、次数保留、两个创建入口拒绝、冷却判定与群内解封；本轮未执行真实 PG，不能将此用例计为通过。
- **验证结果**：上表执行的单测、Bot 全量、类型检查及 Emby 测试均通过，详见本轮汇总。

#### R5：已修复——最大作答次数使用共用常量

- **改动文件**：`apps/bot/src/misakabot/domain.py`、`repository.py`、`onboarding.py`；测试为 `apps/bot/tests/test_verification_departure.py`，以及原有答题/恢复测试。
- **做法与理由**：在 domain 定义 `MAX_GROUP_CHALLENGE_ATTEMPTS = 3`，普通裁决的默认最大次数、管理员处理结果中的剩余次数、恢复题卡中的剩余次数均引用此常量。domain 不依赖业务服务，repository 与 onboarding 可以共同引用，不引入循环导入。数值仍为 3，答错换题、第三次失败和原有文案行为不变。
- **新增测试**：`test_common_attempt_limit_matches_normal_admin_and_recovery_results` 同时检查普通裁决默认值、答错提示、管理员裁决剩余次数与恢复题卡剩余次数。原有第三次失败、错误次数不重置、防重放测试继续通过。
- **验证结果**：Bot 全量测试和 basedpyright 均通过。

#### PostgreSQL 集成验证：未执行

本轮 `MISAKABOT_TEST_DATABASE_URL` 未设置，本机未发现可直接使用的 postgres/initdb；没有可用的独立测试库。未读取或使用生产 DSN，未连接生产库。7 项真实 PG 集成测试均跳过，包括上一轮要求的 `test_admin_adopts_inflight_message_with_typed_snapshot`、第二轮冻结预算测试及本轮退群用例。PG 模拟连接的迁移检查不能替代真实 PG 验证；上线前仍需在独立测试库设置该变量并跑通 Bot 测试命令。

#### P3 与交付状态

P3 仍未实施，原评估与方案保留。未部署、未提交、未推送；原有未提交修改保留，HEAD 仍为 `7ba12ac`。

---

## 第四轮：真实 PostgreSQL 测试暴露的问题（2026-10-24）

R4、R5 已复核通过。"答错两次 → 退群 → 重进"的复现脚本确认：成员会被封禁，重进后不会收到新题。本地复跑 Bot 224 项通过、7 项跳过；basedpyright 0/0；Emby 9 项通过。

### 本轮真实 PG 验证结果（Claude 执行）

- **环境**：本机现有的 `postgres:16-alpine` 镜像，起一个临时容器（只监听 127.0.0.1，`--rm`），用完已删除。没有连接任何现有数据库或生产库。
- **7 项 PG 集成测试**：6 项通过；`test_departure_claim_retry_cooldown_and_join_request_guards` 报错，原因见下面 R6。在临时副本里给该用例补上参数后，7 项全部通过。
- **升级迁移**：先用当前已部署版本 `7ba12ac` 的代码建库，并写入一条待验证记录；再用新代码连续执行两次 `initialize()`。结果：旧记录完整保留；`verification_remaining_seconds`（integer）、`verification_ban_until`（timestamptz）、`scheduled_message_deletions.verification_user_id`（bigint）三列正确补上；重复初始化不报错。

### R6（中）：`delivery_deadline` 默认值为空串，在 PostgreSQL 上会报错

- **现状**：`repository.py` 里 `expired_pending_verifications(now, delivery_deadline="")` 和 `claim_expired_verification(..., delivery_deadline="")` 的默认值是空字符串。SQLite 拿字符串比较不会出问题；但 PostgreSQL 的 `verification_expires_at` 是 `TIMESTAMPTZ`，执行 `verification_expires_at <= ''` 会直接报错：
  ```
  psycopg.errors.InvalidDatetimeFormat: invalid input syntax for type timestamp with time zone: ""
  ```
- **影响**：目前生产代码在 `onboarding.py` 里的两处调用都显式传了这个参数，线上不会触发。但只要有调用方漏传，就会在线上报错，而 SQLite 测试发现不了。`test_postgres_integration.py` 第 148 行就是因为漏传而中途报错，这条用例后半段的断言（审批入群冷却、群内解封等）从来没有在真实 PG 上执行过。
- **期望**：
  - 去掉这两个方法的默认值，把 `delivery_deadline` 改成必填参数（关键字参数也可以），让漏传在类型检查和调用时就暴露出来。
  - 修正 `test_postgres_integration.py` 第 148 行的调用：传入真实的截止时间，例如 `(self.now - timedelta(minutes=30)).isoformat()`，语义和 `OnboardingService._undelivered_challenge_deadline` 保持一致。
  - 顺带检查 repository 里还有没有其他"拿空串或哨兵字符串去和时间戳列比较"的默认参数或写法，有的话一并改掉，并在记录里列出来。
  - 不改变现有行为：未送达的题卡仍然只在截止时间过去 30 分钟后才进入超时处理。
- **测试**：
  - 所有调用点（包括测试）都显式传入 `delivery_deadline`，basedpyright 仍为 0/0。
  - 在 SQLite 下补一个单测：不传这个参数时会直接报错（例如 `TypeError`），而不是静默得到一个错误结果。
  - 有独立 PG 测试库时跑通 7 项集成测试。没有的话如实写"未执行"。也可以照上面的办法，用本机已有的 postgres 镜像起一个临时容器（只绑 127.0.0.1，用完删除），**不要连接生产库**。

### 部署提醒（不是代码修改）

本批改动新增了两个状态 `VERIFICATION_DEPARTURE_PENDING`、`VERIFICATION_DEPARTURE_BANNED`，还给数据库加了列。Bot 和审计 API（audit-api）必须一起更新；旧版程序读到这两个新状态会解析失败。

### 第四轮修复记录（由 Codex 填写）

<!-- 改动文件、做法、新增测试、验证命令和结果；PG 测试写明在什么环境执行 -->

#### R6：已修复——超时查询和认领必须显式传入投递截止时间

- **改动文件**：`apps/bot/src/misakabot/repository.py`、`apps/bot/tests/test_onboarding.py`、`test_postgres_integration.py`、`test_repository_recovery.py`。
- **做法**：移除 `expired_pending_verifications` 和 `claim_expired_verification` 的 `delivery_deadline=""` 默认值，改为必填 `str` 参数，并更新前者的说明，去掉依赖空串排除未送达题卡的说法。不改查询 SQL、状态认领条件或 30 分钟策略。`OnboardingService` 的两处生产调用原本就显式使用 `_undelivered_challenge_deadline`，保持不变。补齐现有 SQLite 测试的正常调用；PG 退群用例原第 148 行按其实际认领时刻 `expires` 减去 `OnboardingService.UNDELIVERED_CHALLENGE_GRACE_MINUTES`，传入真实 ISO 截止时间，保证与服务策略一致。
- **新增测试**：`test_expiry_methods_require_delivery_deadline_before_opening_sqlite` 在 SQLite 仓储下故意漏传参数，分别验证两个方法立即抛出含 `delivery_deadline` 的 TypeError，且 `_connect` 从未被调用；正常调用均显式传参，只有该负向测试故意省略。`test_undelivered_expiry_and_claim_preserve_thirty_minute_grace_boundary` 验证未送达题卡在原截止时间过去 29 分 59 秒时既不被列出、也不能认领，恰好过去 30 分钟时两者均可；已送达题卡和第一阶段申请仍按自己的原截止时间处理。

#### 仓储时间戳比较排查

- **发现并修复的两处**：`expired_pending_verifications`、`claim_expired_verification` 的空串时间默认值，均已移除，没有改为其他哨兵日期。
- **未发现其他同类写法**：检查了仓储全部字符串默认参数、空串/日期哨兵/COALESCE，以及涉及 `verification_expires_at`、`verification_ban_until`、`updated_at`、`requested_at`、`due_at`、`leased_at`、`processed_at`、`last_notice_at` 的比较。其余时间窗口由必填时间参数或业务计算的实际 ISO 时间传入；未使用 `''`、极早/极晚日期或 `infinity` 来代替截止时间。
- **保留的非问题写法**：`COALESCE(display_name, '')` 只用于展示名称，不参与时间戳比较；其余 COALESCE 用于 token、群 ID、清理队列成员归属。`retry_before=None` 的可选流程控制也未替换成空串或伪造日期，本轮不改变其语义。

#### 验证结果

开始修改前先运行开头三条命令：Bot 231 项中 224 项执行通过、7 项 PG 跳过；basedpyright 0 errors / 0 warnings；Emby 9 项通过。修改后仍按这三条命令运行：

| 命令（在仓库根目录分别执行） | 结果 |
| --- | --- |
| `cd apps/bot && PYTHONPATH=src:tests ../../.venv/bin/python -m unittest discover -s tests` | 发现 233 项：226 项执行通过，7 项 PG 集成测试因该命令未设置测试 DSN 默认跳过；退出码 0 |
| `cd apps/bot && ../../.venv/bin/basedpyright --pythonpath ../../.venv/bin/python src` | 0 errors / 0 warnings / 0 notes；退出码 0 |
| `cd apps/emby-manager && PYTHONPATH=src ../../.venv/bin/python -m unittest discover -s tests` | 9 项执行通过；退出码 0 |

`git diff --check` 通过；原有 Starlette 测试客户端弃用提示仍存在。测试和类型检查未退化。

#### 本轮真实 PostgreSQL 集成测试：7 项全部通过

- **环境**：macOS 主机上的 Docker 29.4.1；使用本机已存在的 `postgres:16-alpine` 镜像（`--pull=never`）。容器内实际版本为 **PostgreSQL 16.15 / aarch64 / Alpine**。
- **隔离**：新建本轮专用的 `misakabot-r6-pg-1792815652552` 容器，使用 `--rm`、内存临时数据目录 `/var/lib/postgresql/data`，仅发布到 **127.0.0.1:53753 → 5432**。新建临时测试用户和数据库 `misakabot_test`，没有挂载项目或生产数据目录，未使用现有数据库或生产 DSN。
- **执行**：仅在测试命令的进程环境中设置 `MISAKABOT_TEST_DATABASE_URL` 为上述本机新容器的测试 DSN，在 `apps/bot` 执行 `PYTHONPATH=src:tests ../../.venv/bin/python -m unittest test_postgres_integration -v`。测试依然只写连接内 TEMP 表。
- **结果**：7 项执行通过、0 项跳过、退出码 0。包括原来报错的 `test_departure_claim_retry_cooldown_and_join_request_guards`，其后半段的申请入群重置保护、冷却判定与群内解封断言均已实际执行通过；上一轮管理员认领题卡、暂停预算恢复、原生值、审计与记忆等 PG 用例也全部通过。
- **清理**：测试结束后已删除本轮临时容器，临时数据库随容器销毁。没有连接生产库，也没有对任何既有数据库执行初始化或迁移。

#### 交付边界

P3 仍未实施；本轮没有新增数据库列或改动迁移。未部署、未提交、未推送，先前未提交修改保留。第四轮上方关于 Bot 与 audit-api 必须一起更新的部署提醒仍然有效，本轮未执行部署。
