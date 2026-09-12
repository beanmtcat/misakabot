# Emby Manager Service

独立的 Emby 用户与观看记录管理 API。它不依赖 MisakaBot 的 Python 包、启动入口或配置。

数据库表保持不变：服务只读写已有的用户、观看记录与电视剧索引表；不包含任何 DDL 或 migration。

```bash
cp .env.example .env
# 填写 Emby、会话密钥与 PostgreSQL 配置
python -m pip install -e .
cd web && npm install && npm run build && cd ..
emby-manager-api
```

或使用 Docker：

```bash
cp .env.example .env
docker compose up -d --build
```

服务默认监听 `127.0.0.1:8084`。访问 `/emby-manager/` 即可打开独立 React 浏览器后台，
不依赖 Telegram Mini App。它复用 `dragonli_users` 中 `islogin=true` 用户的 PHP bcrypt 密码，
并创建本服务自己的 HttpOnly 会话 Cookie；不读取或改写 Dragonli 的 session / remember token。

因此，直接访问地址为 `http://127.0.0.1:8084/emby-manager/`，生产环境为
`https://your-domain/emby-manager/`。反向代理应将这个路径原样转发到服务，不能剥离
`/emby-manager` 前缀。

Docker 镜像会自动使用 `/app/web/dist`。仅在非 Docker 部署且前端构建文件不在项目默认
`web/dist` 目录时，才需要设置 `EMBY_WEB_ROOT`。

用户、登录会话与播放活动会在服务启动后立即同步；电视剧索引会等待周期后执行，默认每 30 分钟同步用户和电视剧、
每 1 分钟同步登录会话和播放活动。可通过
`EMBY_USER_SYNC_INTERVAL_SECONDS`、`EMBY_SERIES_SYNC_INTERVAL_SECONDS`、
`EMBY_LOGIN_SYNC_INTERVAL_SECONDS` 和 `EMBY_WATCH_SYNC_INTERVAL_SECONDS` 调整周期。

设置 `TMDB_API_TOKEN` 后，服务会每 6 小时同步已开启追更且已设置 TMDB ID 的电视剧，更新官方已播集数、
下次播出日期及本服集数。服务启动时不会额外执行 TMDB 同步；可通过 `EMBY_TRACKING_SYNC_INTERVAL_SECONDS`
调整周期。

同时设置 `MOVIEPILOT_BASE_URL` 与 `MOVIEPILOT_API_TOKEN` 后，服务会立即读取 MoviePilot 的电视剧订阅，
之后每 30 分钟导入一次。已存在的电视剧会开启追更；TMDB ID 尚未存在的电视剧会新建到现有
`dragonli_emby_series` 表并直接标记为追更中。新建记录使用负数 ID，以免与 Emby 同步的正数 ID 冲突。
当同一部剧从 Emby 同步到服务时，会以 `themoviedb` 匹配并替换该占位记录，保留其追更状态；不会删除
任何实际 Emby 剧集记录。可通过 `MOVIEPILOT_SYNC_INTERVAL_SECONDS` 调整周期。
服务使用 MoviePilot 的 `/api/v1/subscribe/` 订阅清单接口，并通过 `X-API-KEY` 请求头认证。

- `GET /emby-manager/`：管理后台；
- `GET /emby-manager/v1/emby/users`：查询用户；
- `POST /emby-manager/v1/emby/users/sync`：从 Emby 同步用户；
- `PATCH /emby-manager/v1/emby/users/{user_id}/policy`：禁用/启用用户或切换远程访问；
- `GET /emby-manager/v1/emby/dashboard`：媒体库与活动概览；
- `GET /emby-manager/v1/emby/movies`：查询已有电影索引及观看次数；
- `GET /emby-manager/v1/emby/series`：查询已有电视剧、季和单集索引；
- `POST /emby-manager/v1/emby/series/sync`：从 Emby 同步电视剧，并按 TMDB ID 合并追更状态；
- `PATCH /emby-manager/v1/emby/series/{series_id}/tracking`：开启或停止已有电视剧的追更；
- `POST /emby-manager/v1/emby/series/tracking/sync`：立即同步已追更电视剧的 TMDB 数据；

该接口在配置 MoviePilot 后会先导入其电视剧订阅，再同步 TMDB 追更数据。
- `GET /emby-manager/v1/emby/watch-logs`：查询观看记录；
- `POST /emby-manager/v1/emby/watch-logs/sync`：同步活跃会话并结束已离开的会话。
- `GET /emby-manager/v1/emby/login-logs`：查询 Emby 登录日志；
- `POST /emby-manager/v1/emby/login-logs/sync`：将当前 Emby 会话按会话 ID 去重写入登录日志。
- `GET /emby-manager/v1/emby/items/{item_id}/open`：跳转至 Emby Web 的媒体详情页。

`GET /emby-manager/v1/emby/nodes/{node_id}/series-paths` 供远端转存脚本拉取路径映射，不使用
Cookie 或 Bearer Token。请求必须使用 `EMBY_PATH_MAP_API_SECRET` 计算 HMAC-SHA256，并附带：

- `X-Path-Map-Timestamp`：Unix 秒级时间戳（容许前后 5 分钟）；
- `X-Path-Map-Nonce`：每次请求唯一的随机值；
- `X-Path-Map-Signature`：对 `GET + "\\n" + 原始路径 + "\\n" + 查询串 + "\\n" + timestamp + "\\n" + nonce` 的 HMAC-SHA256 十六进制摘要。

服务端会拒绝过期、重复 nonce 或签名不匹配的请求。`auto_transfer.py` 已自动生成这些请求头；其
`path_map_api` 配置应使用 `secret_env` 引用环境变量名，例如 `"secret_env": "EMBY_PATH_MAP_API_SECRET"`。

`EMBY_BASE_URL` 仅用于服务端访问 Emby API。若 API 使用内网地址，请设置 `EMBY_WEB_BASE_URL` 为浏览器可访问的 Emby 公网地址，媒体名称跳转会使用该地址。
