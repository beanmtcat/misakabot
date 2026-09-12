# Misaka Guard 审计 Mini App

Telegram 群管理员在机器人菜单中打开的审计日志界面。首版包含事件筛选、搜索、证据详情及“确认封禁/误封恢复”交互。

部署后的审计入口为 `/audit/`，例如 `https://你的域名/audit/`。

## 接入 Python 机器人后端

前端不能直接访问数据库，也不能仅根据 Mini App 浏览器传入的用户 ID 授权。所有数据与操作均由 Python 后端处理：

```text
Telegram Mini App
  └─ HTTPS API（携带原始 Telegram WebApp initData）
       └─ Python API：校验 initData HMAC → 检查管理员 user_id → 审计数据库
```

建议 API：

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/v1/audit/events` | 分页读取日志，支持状态、风险、关键词与时间筛选 |
| `GET` | `/v1/audit/events/{event_id}` | 读取原文、OCR、模型输出与处置记录 |
| `POST` | `/v1/audit/events/{event_id}/confirm-ban` | 管理员确认永久封禁 |
| `POST` | `/v1/audit/events/{event_id}/restore` | 解除限制并标记误封 |
| `GET` | `/v1/audit/summary` | 读取今日拦截、待审核与封禁计数 |

每个请求使用 `X-Telegram-Init-Data` 传递 Telegram Web App 的原始 `initData`。后端应以 Bot Token 校验其 HMAC，验证 `auth_date` 时效，并只允许 `ADMIN_USER_IDS` 中的 Telegram 用户访问或操作。所有写操作还须记录操作者、请求时间和原动作。

在将真实 API 地址写入应用前，界面仅展示匿名化演示数据；不得将 Bot Token、数据库密码或任意管理员名单放进前端代码。
