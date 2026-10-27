# SimpleURL

> 轻量、流畅、好用的高级短链接系统。粘贴链接，回车即得。

自托管、单文件部署、自带访问统计与二维码。技术栈：**FastAPI + SQLAlchemy + SQLite + 原生前端**，无构建步骤。

## 特性

- **一键生成** — 粘贴长链接回车即得短码，也可自定义短码
- **访问模式** — 开放外部可使用 / 仅管理员可使用 / 邀请码可使用，一键切换
- **安全可靠** — Argon2id 密钥哈希、CSP nonce、全套安全响应头、SSRF 防护、防刷限额、会话闲置超时
- **访问控制** — 口令短码（打开前验证）、访问次数上限、每日次数上限，适合不该被随手转发的链接
- **域名名单** — 目标域名黑白名单，创建、编辑、迁移、跳转四处同一套判定
- **访问统计** — 总点击 / 独立访客 / 30 天趋势图 / 来源 Top，按 IP 哈希脱敏
- **二维码** — 每条链接自动生成 PNG 二维码
- **创建前校验** — 自定义短码边打边查占用，目标地址重复时提示复用已有短码
- **标签管理** — 标签检索、状态筛选、多字段搜索、多种排序
- **链接控制** — 随时停用 / 启用 / 删除，支持设置过期时间
- **管理后台** — `/admin` 六面板：仪表盘、链接管理、防洪/UA、API 密钥、数据迁移、设置
- **防洪** — 单 IP / 单短码多维限速、UA 黑名单、微信与 QQ 内置浏览器兼容处理
- **延迟计数** — 点击先入内存缓冲，按间隔批量落库，热链接不会把库写爆
- **自动取标题** — 创建时抓目标页 `<title>`，带 SSRF 防护，抓不到用 URL 兜底
- **精致界面** — 深色玻璃质感、响应式、无框架依赖、极光动效
- **无背景 Logo** — 提供 SVG 矢量与 16~512px PNG，深浅底色通用

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 生成两个密钥（生产环境务必做）
export SECRET_KEY=$(openssl rand -hex 32)      # 会话签名
export ADMIN_TOKEN=$(openssl rand -hex 24)     # 管理密钥

# 3. 启动
./run.sh                     # 默认 http://localhost:8000
```

或使用 Docker：

```bash
docker compose up -d         # 首次需构建
```

首次启动自动建表（SQLite），无需任何额外配置。

## 界面

| 页面 | 地址 |
| --- | --- |
| 首页（创建 + 链接库） | `/` |
| 管理后台（六面板） | `/admin` |
| 管理员登录 | `/admin/login` |
| 链接统计 | `/s/{短码}` |
| 健康检查 | `/healthz` |
| API 文档（DEBUG 模式） | `/api/docs` |

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/links` | 列表，支持 `q`/`tag`/`status`/`sort`/`page`/`page_size` |
| `POST` | `/api/links` | 创建，`target` 必填，`code` 留空自动生成 |
| `GET` | `/api/links/{code}` | 详情 |
| `PATCH` | `/api/links/{code}` | 更新（短码不可改） |
| `DELETE` | `/api/links/{code}` | 删除 |
| `GET` | `/api/links/{code}/stats` | 访问统计 |
| `GET` | `/api/codes/{code}/available` | 自定义短码可用性预检 |
| `GET` | `/api/admin/duplicate?target=` | 按目标地址查已有短码（管理员） |
| `GET` | `/api/qr/{code}` | 二维码 PNG |
| `GET` | `/api/stats/summary` | 总览 |
| `GET` | `/{code}` | 跳转（307），自动记录点击 |
| `GET` | `/api/access` | 当前访问模式与身份 |
| `GET` | `/api/announcement` | 站点公告 |
| `GET` | `/api/admin/overview` | 仪表盘数据（管理员） |
| `GET/POST` | `/api/admin/settings` | 读取 / 保存运行配置（管理员） |
| `GET/POST` | `/api/admin/keys` | API 密钥列表 / 创建（管理员） |
| `POST` | `/api/admin/migrate` | YOURLS 数据导入（管理员） |
| `GET` | `/api/admin/export` | 导出全部链接 JSON（管理员） |
| `POST` | `/api/admin/flush` | 立即结算点击缓冲（管理员） |
| `POST` | `/api/admin/login` | 管理员登录（`X-Admin-Token` 头） |
| `POST` | `/api/admin/logout` | 退出登录 |
| `GET` | `/api/admin/invite` | 查看当前邀请码（管理员） |
| `POST` | `/api/invite?code=` | 用邀请码解锁创建 |
| `POST` | `/api/links/bulk` | 批量 `enable` / `disable` / `delete`，一次最多 200 条 |
| `GET` | `/api/board` | 公开热门榜：短码 + 标题 + 点击数，不含目标地址 |

> 列表、详情、更新、删除、统计均为管理员接口；未登录时返回 401。
> 创建接口受访问模式约束；跳转始终对所有人开放。

创建示例：

```bash
curl -X POST http://localhost:8000/api/links \
  -H 'Content-Type: application/json' \
  -d '{"target":"https://example.com/very/long/url","code":"my-link","tags":["文档"]}'
```

创建时可选的三个「限制类」字段：

| 字段 | 说明 |
| --- | --- |
| `expires_at` | 过期时间，ISO 8601 |
| `max_clicks` | 访问次数上限，点满即失效（做限量券、限量报名很好用） |
| `daily_clicks_limit` | 每日次数上限，次日自动恢复（限量券别一秒钟被抢光） |
| `password` | 访问口令，打开前先进验证页；同一链接一次一验，不留长期通行证 |

更新（`PATCH`）用 `clear_expiry` / `clear_max_clicks` / `clear_daily_limit` / `clear_password`
四个显式开关来「清除」，避免和「不修改」混在一起。

### 创建前校验

两条都是「顺手」而非「强制」，提交时后端仍会再校验一遍：

- `GET /api/codes/{code}/available` — 短码能不能用。字符集、长度、大小写不敏感的重名都会先说清楚。
  非管理员且当前模式下不可创建时，一律回「可以用」，避免这个接口变成短码库探测器。
- `GET /api/admin/duplicate?target=` — 这个地址是不是已经有一条短码了。
  比对时忽略协议差异、末尾斜杠与 `#` 片段，所以 `http://a.com/x`、`https://a.com/x/`
  会被认成同一条；结果按点击量排序，直接复制复用即可。

## 防洪与微信 / QQ

短链最容易被两件事搞死：被人当跳板刷量，以及一条链接在微信群里被引爆。

### 限速

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `GUARD_IP_PER_MINUTE` | 120 | 单 IP 每分钟跳转上限，超出返回 429 友好页 |
| `GUARD_IP_DAILY` | 20000 | 单 IP 每日点击配额 |
| `GUARD_CODE_PER_MINUTE` | 0 | 单短码每分钟上限，0 不限制 |
| `GUARD_BLOCK_SECONDS` | 60 | 命中后封禁多久 |

任何一项设为 `0` 即关闭。所有阈值都能在 `/admin/guard` 里改，落库后立即生效、重启不丢，
且**数据库里的值优先于环境变量**。

超限不是返回裸 JSON，而是一张告诉你「大约多少秒后解除」的页面。

### UA 屏蔽

`BLOCKED_UAS` 支持逗号分隔的多条规则，支持子串与 `*` 通配：

```
BLOCKED_UAS=*bot*,curl/,SemrushBot
```

**命中后仍然正常跳转，只是不计入点击**，并在后台留痕。理由很直接：
统计是给运营看的，不该成为访客能否打开链接的开关。

### 微信 / QQ 内打开

这两个 App 的内置浏览器会拦掉不少落地页，用户看到白屏就走了。默认策略是**中转页**：

- `WECHAT_MODE` / `QQ_MODE` = `warn`（默认）— 给一张带「复制链接」「仍是继续访问」的页面
- `blocked` — 一律拦截，返回 403 与复制按钮
- `off` — 不做任何处理，直接 307

微信 UA 里常带 QQ 浏览器的内核标记，判定时微信优先，不会被误判成 QQ。

## 口令短码与访问上限

有些链接不该被随手转发，有些链接只能被前 100 个人打开。这两件事都不需要外部服务：

```bash
# 需要口令，且最多被打开 50 次
curl -X POST http://localhost:8000/api/links -H 'X-Admin-Token: ...' \
  -H 'Content-Type: application/json' \
  -d '{"target":"https://example.com/report","code":"vip","password":"letmein","max_clicks":50}'
```

- 口令用 **Argon2id** 哈希存储，库里没有明文；验证页不会泄露目标地址
- 口令正确后 303 回跳，并在本次 URL 上带一次性标记 `_su_ok=1`，
  不在浏览器里留通行证——一条链接一次一验，别处拿到地址也打不开
- 验证失败与登录共用一个限速器（默认 5 分钟 10 次），挡得住口令爆破
- 次数上限只统计「真的跳转过」的点击，被 UA 屏蔽或限速拦下的不计入

### 每日次数上限

`max_clicks` 管总量，`daily_clicks_limit` 管节奏。做限量券时，前者定总量，
后者防止「一秒钟就被抢完」：

```json
{"target": "https://example.com/coupon", "max_clicks": 1000, "daily_clicks_limit": 100}
```

- 按 **UTC 日切**，跨天自动清零，没有任何定时任务：日期对不上就等于没用过
- 额度与点击一起走内存缓冲，一次原子记账。分两处更新的话，同一秒的并发请求
  会集体看到「今天还剩 1 次」，把额度超卖一整天
- 后台列表与统计页显示的「今日已用」含尚未落库的部分，不会出现
  「显示 0/5 却跳不动」这种说不清的 bug
- 额度用完返回 410 与「今日额度已用完」页面，`state` 变成 `daily`

链接状态收敛成一个字段，列表、统计页、跳转、导出都用它，不会各处各判一套：

| `state` | 含义 |
| --- | --- |
| `active` | 正常可用 |
| `disabled` | 被管理员停用 |
| `expired` | 超过过期时间 |
| `exhausted` | 次数已用完 |
| `daily` | 今日额度已用完，次日恢复 |

## 热门榜与批量操作

- **热门榜** — 首页按累计点击列出前 8 条，访客可见，但只露短码、标题与点击数。
  停用 / 过期 / 点满 / 目标被拉黑的条目自动落榜，不留一个跳不动的坑。
  条数由运行配置 `board_size`（3–20）控制。
- **批量操作** — 链接库支持勾选后批量启用 / 停用 / 删除，逐条处理并回报
  成功与缺失（写错的短码不会让整批失败）。改目标地址这类需要逐条确认的事
  不进批量——点错一次就全废了，不值得图这点快。

## 目标域名名单

`BLOCKED_HOSTS` / `ALLOWED_HOSTS`，也可在后台「设置」里改，域名级判定：

```
BLOCKED_HOSTS=spam.example,*.shortener.evil
ALLOWED_HOSTS=
```

- 裸域名 `a.com` 连同其所有子域一起生效；`*.a.com` 只匹配子域，不含 `a.com` 本身
- `ALLOWED_HOSTS` 一旦非空即切换成白名单模式：只放行名单内的域名
- 判定只有一处实现，**创建、编辑、YOURLS 迁移、实际跳转**都走它。
  事后把域名拉黑，存量短链也会立刻停止跳转，而不是继续靠旧码当跳板

## 点击计数

一次点击原先就是一次 `INSERT` + 一次 `UPDATE`，热链接能把 SQLite 写满。现在改成：

1. 点击先入内存队列
2. 攒够 `CLICK_FLUSH_BATCH`（默认 200）条，或过了 `CLICK_FLUSH_INTERVAL`（默认 5）秒，统一批量落库
3. 一批只走一个事务，点击数用一条 `UPDATE ... SET clicks = clicks + n` 合并
4. 写库失败整批退回队列；进程正常退出（含 SIGTERM）会强制结算一次

代价写在明面上：`kill -9` 会丢掉缓冲里最后几秒的点击。要绝对不丢就把间隔调到 1 秒。

后台「仪表盘」会单独显示「待落库」条数，也能手动点「立即结算缓冲」。

## 配置

所有配置项均可用环境变量覆盖，见 [`.env.example`](.env.example)。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SITE_NAME` | `SimpleURL` | 站点名称 |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | 监听地址 |
| `DATABASE_URL` | SQLite | 支持 Postgres：`postgresql+psycopg://...` |
| `CODE_LENGTH` | `6` | 自动生成短码长度 |
| `CODE_MIN_LENGTH` / `CODE_MAX_LENGTH` | `3` / `16` | 自定义短码长度限制 |
| `CREATE_DAILY_LIMIT` | `200` | 单 IP 每日创建上限，`0` 不限制 |
| `DEBUG` | `false` | 开启后开放 `/api/docs` |
| `ACCESS_MODE` | `public` | 访问模式：`public` / `admin` / `invite` |
| `ADMIN_TOKEN` | 空 | 管理密钥，留空则管理功能关闭；可直接填 Argon2id 哈希 |
| `ADMIN_ONLY` | `false` | 旧版开关，等价于 `ACCESS_MODE=admin` |
| `SECRET_KEY` | 随机 | 会话签名密钥，与 `ADMIN_TOKEN` 无关；改它等于全站重新登录 |
| `CSP_REPORT_ONLY` | `false` | CSP 只上报不拦截，上线初期用于观察误伤 |
| `TRUSTED_PROXIES` | 空 | 可信反代地址，逗号分隔；留空则不采信 `X-Forwarded-For` |
| `GUARD_IP_PER_MINUTE` / `GUARD_IP_DAILY` | `120` / `20000` | 防洪限速，`0` 关闭 |
| `GUARD_CODE_PER_MINUTE` | `0` | 单短码每分钟上限 |
| `BLOCKED_UAS` | 空 | UA 黑名单，逗号分隔，支持 `*` 通配 |
| `WECHAT_MODE` / `QQ_MODE` | `warn` | 内置浏览器策略：`warn` / `blocked` / `off` |
| `CLICK_FLUSH_INTERVAL` / `CLICK_FLUSH_BATCH` | `5` / `200` | 延迟计数落库节奏 |
| `SESSION_IDLE_TIMEOUT` | `7200` | 后台会话闲置多久失效（秒），用着会自动续期 |
| `BLOCKED_HOSTS` / `ALLOWED_HOSTS` | 空 | 目标域名黑/白名单，`a.com` 连同子域生效 |
| `RETENTION_DAYS` | `180` | 点击明细保留天数，超期自动清理，累计点击数不动 |
| `BOARD_SIZE` | `8` | 首页热门榜条数（3–20） |

## 访问模式

站点有三种访问策略，通过 `ACCESS_MODE` 切换，无需改代码：

| 模式 | 访客能做 | 管理员能做 |
| --- | --- | --- |
| `public` 开放外部可使用 | 创建短链接、跳转 | 创建 + 管理全部链接 |
| `admin` 仅管理员可使用 | 只能跳转 | 全部 |
| `invite` 邀请码可使用 | 凭邀请码创建、跳转 | 全部，并可签发邀请码 |

- 管理操作（链接库、详情、改、删、统计页、二维码）任何模式下都只认管理员
- 管理员登录入口 `/admin/login`，密钥即 `ADMIN_TOKEN`
- 未配置 `ADMIN_TOKEN` 时管理功能关闭，`admin` / `invite` 会自动退回 `public`，不会把站点锁死
- 邀请码由 `ADMIN_TOKEN` + 日期派生，每天滚动，登录后点顶栏「邀请码」即可复制
- 登录态是 HMAC 签名的 Cookie，有效期 7 天，无需额外会话存储

```bash
# 仅管理员可使用
ACCESS_MODE=admin ADMIN_TOKEN=$(openssl rand -hex 24) ./run.sh

# 开放外部可使用（管理仍需登录）
ACCESS_MODE=public ADMIN_TOKEN=$(openssl rand -hex 24) ./run.sh
```

## Logo

位于 `app/static/logo/`，全部**纯透明背景、无底板**，深浅底色都能直接用：

- `icon.svg` / `icon-{16,32,48,64,96,128,180,192,256,512}.png` — 主图标
- `favicon.svg` / `../favicon.ico` — 站点图标（画布收窄，小尺寸更清楚）
- `icon-mono.svg` — 单色版，用 `currentColor` 即可换色
- `logo-horizontal.svg` — 图标 + 字标横版

造型是一条从右上绕回、再把去路折成箭头的折返线，讲的就是「长链接被绕短」。
渐变沿对角线铺，折返处自然衔接深蓝 → 紫 → 品红。

重新生成（需要 `cairosvg` + `Pillow`）：

```bash
python scripts/gen_logo.py
```

## 测试

```bash
pip install -r requirements-dev.txt
pytest
```

198 个用例覆盖创建校验、跳转、停用/过期、列表筛选排序、更新删除、统计与二维码，
每日额度（含缓冲未落库时的并发超卖）、批量操作、公开热门榜，
三种访问模式、登录登出与鉴权绕过，安全加固专项（响应头、CSP nonce、Argon2id 哈希与降级、
会话绑定与轮换、Cookie 属性、IP 信任边界、开放跳板），以及本轮新增的防洪专项：
限速 429、UA 屏蔽不计数、微信/QQ 三种策略、延迟计数批量落库，后台各面板与运行配置，
以及短码预检与目标查重（大小写、斜杠、片段归一化，访客不泄露占用情况）。

## 项目结构

```
app/
├── main.py            入口：路由装配、异常处理
├── api.py             REST API
├── pages.py           跳转 / 统计页 / healthz
├── services.py        业务逻辑层
├── models.py          数据模型
├── schemas.py         请求响应模型
├── qr.py              二维码生成
├── web.py             页面渲染
├── admin.py           管理后台页面与接口
├── admin_service.py   仪表盘聚合 / API 密钥 / YOURLS 迁移
├── core/              config / runtime / db / codes / crypto / security / auth
│                      / guard / clickbuffer / fetcher / middleware / timeutil
├── templates/         Jinja2 模板
├── static/            CSS / JS / Logo
└── tests/             pytest 用例
```

## 安全设计

### 密钥与登录

- 管理密钥用 **Argon2id**（m=19MiB, t=2, p=1）哈希存储。`ADMIN_TOKEN`
  既可以写明文，也可以直接填哈希串——填哈希的话，配置文件泄露也拿不回原密钥：

  ```bash
  python -c "from app.core.crypto import hash_secret; print(hash_secret('你的密钥'))"
  ```

  环境缺 `argon2-cffi` 时自动退回 PBKDF2-HMAC-SHA256（60 万次迭代），
  两种编码格式互不兼容但接口一致，检测到参数偏低会提示重新哈希。
- 密钥比对一律恒定时间，不泄露时序信息。
- 登录密钥**只接受 `X-Admin-Token` 请求头**，不接受 URL query——
  query 会进访问日志、浏览器历史和 Referer，等于把钥匙抄在门上。
- 登录接口按 IP 限速（5 分钟 10 次），超限返回 429。
- 会话是 HMAC-SHA256 签名的 HttpOnly Cookie，服务端不存会话。
  有效期与密钥指纹都签进令牌，所以轮换 `ADMIN_TOKEN` 或 `SECRET_KEY`
  会让旧 Cookie 立刻全部失效，不需要维护黑名单。

### 响应头与 CSP

所有响应统一带：

| 头 | 作用 |
| --- | --- |
| `Content-Security-Policy` | 默认 `'self'`，脚本走 nonce 模式 |
| `X-Content-Type-Options: nosniff` | 禁掉 MIME 嗅探 |
| `X-Frame-Options: DENY` | 禁点击劫持 |
| `Referrer-Policy` | 站外只传 origin |
| `Permissions-Policy` | 关掉定位/摄像头/麦克风等强权限 |
| `Cross-Origin-Opener-Policy` / `Cross-Origin-Resource-Policy` | 隔离跨域窗口与资源 |
| `Strict-Transport-Security` | 仅 https 下发送，两年有效期 |

CSP 用 **nonce** 而非 hash 白名单：每次请求生成随机 nonce，模板里的
`<script>` 必须带上它才放行。这意味着任何通过字符串拼进页面的事件处理器
都会失效——这是有意的收紧。`style-src` 保留 `'unsafe-inline'`，
因为图表柱高、二维码尺寸要靠内联样式。

上线初期建议先开 `CSP_REPORT_ONLY=true` 观察会不会误伤，稳定后再关。

### IP 与限速

`X-Forwarded-For` / `X-Real-IP` **默认不采信**——任何人都能伪造它们，
采信了就等于给限速和 IP 配额开后门。只有把反代地址写进 `TRUSTED_PROXIES`
后才会从这些头里取真实客户端地址，且取最右侧一个非信任节点。

```bash
TRUSTED_PROXIES=127.0.0.1,10.0.0.1 ./run.sh
```

### 会话

- 签名 Cookie 的 7 天是「最迟失效」，另叠一层闲置超时（`SESSION_IDLE_TIMEOUT`，默认 2 小时）：
  公用电脑上签完就走不会一直挂着；持续使用时每次请求会把令牌重签，
  所以用着不会突然掉线
- 登录接口先验密钥、后看身份，带错密钥的请求不会被现有 Cookie 放行；
  失败次数单独计数（15 分钟 20 次即锁定），「错 9 次成功 1 次」刷不空窗口
- 登录限速表每 5 分钟清理一次过期窗口，避免被不同来源刷爆内存
- 页面与 `/api/*` 一律 `Cache-Control: no-store`，中间层缓存不会串号；
  二维码这类自带缓存策略的接口保持原样

### 数据

- 访客 IP 的摘要加了 `SECRET_KEY` 当盐。不加盐的 SHA-256 前缀对 IPv4
  等于没脱敏——地址空间只有 2^32，枚举即得原值
- 点击明细按 `RETENTION_DAYS`（默认 180 天）自动清理，只保留累计点击数；
  清理跑在独立后台线程里，不让访客的请求等一次批量删除

### 其它

- URL 校验拦掉内网 / 回环 / 链路本地地址，自动取标题时同样生效（防 SSRF）
- 纯数字短码一律拒绝：`/8080` 会被浏览器和手机当成端口号，粘贴即失效
- 短码目标强制补成完整绝对 URL，危险协议（`javascript:`、`data:`、`file:`）一律拒绝
- 统计数据只存 IP 的 SHA-256 前 32 位，不落明文
- 停用 / 过期 / 不存在的短码都给友好页面，不裸奔 JSON
- 未配 `ADMIN_TOKEN` 时 `admin` / `invite` 自动退回 `public`，
  避免少写一行配置把整站锁死
