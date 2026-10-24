# SimpleURL

> 轻量、流畅、好用的高级短链接系统。粘贴链接，回车即得。

自托管、单文件部署、自带访问统计与二维码。技术栈：**FastAPI + SQLAlchemy + SQLite + 原生前端**，无构建步骤。

## 特性

- **一键生成** — 粘贴长链接回车即得短码，也可自定义短码
- **安全可靠** — URL 白名单校验、内网地址拦截、短码去重、防刷创建限额
- **访问统计** — 总点击 / 独立访客 / 30 天趋势图 / 来源 Top，按 IP 哈希脱敏
- **二维码** — 每条链接自动生成 PNG 二维码
- **标签管理** — 标签检索、状态筛选、多字段搜索、多种排序
- **链接控制** — 随时停用 / 启用 / 删除 / 编辑，支持设置或清除过期时间
- **数据导出** — 按当前筛选一键导出 CSV，Excel 直接打开不乱码
- **社交平台防洪** — 微信 / QQ / 微博等内置浏览器逐平台放行、静默或拦截，带中转页与来源分布
- **设备统计** — 桌面端 / 移动端 / 机器人来源一目了然
- **可选私密** — 配一个 `ACCESS_TOKEN` 就能把整个站点锁起来
- **安全加固** — CSP、CSRF 防护、Argon2id 口令哈希、登录限速、可信代理
- **精致界面** — 深色玻璃质感、响应式、无框架依赖、极光动效
- **无背景 Logo** — 提供 SVG 矢量与 16~512px PNG，深浅底色通用

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 启动
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
| 管理后台 | `/admin` |
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
| `GET` | `/api/qr/{code}` | 二维码 PNG |
| `GET` | `/api/links/export` | 导出 CSV（沿用列表的筛选参数） |
| `GET` | `/api/stats/summary` | 总览 |
| `GET` | `/api/access` | 当前访问模式与登录态 |
| `POST` | `/api/admin/login` | 后台登录 |
| `GET` | `/api/admin/dashboard` | 仪表盘数据 |
| `GET/POST` | `/api/admin/settings` | 读取 / 保存运行参数 |
| `GET/POST` | `/api/admin/ua-rules` | UA 屏蔽规则 |
| `GET/POST/DELETE` | `/api/admin/api-keys` | API 密钥管理 |
| `POST` | `/api/admin/migrate/yourls` | 从 YOURLS 迁移 |
| `POST` | `/api/admin/flush-clicks` | 立即结算延迟计数 |
| `POST` | `/yourls-api` | YOURLS 兼容接口（shorturl / expand / url-stats / db-stats） |
| `GET` | `/{code}` | 跳转（307），延迟记录点击 |

创建示例：

```bash
curl -X POST http://localhost:8000/api/links \
  -H 'Content-Type: application/json' \
  -d '{"target":"https://example.com/very/long/url","code":"my-link","tags":["文档"]}'
```

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
| `ACCESS_TOKEN` | 空 | 设置后访问短链与统计页需带 `?token=`，留空则公开 |
| `ACCESS_MODE` | `open` | `open` 开放外部可使用；`admin` 仅管理员可使用 |
| `EXPORT_MAX_ROWS` | `50000` | 单次导出 CSV 的最大条数 |
| `DEBUG` | `false` | 开启后开放 `/api/docs` |
| `ADMIN_TOKEN` | 空 | 后台口令，`admin:password` 可分别设置，也支持哈希值 |
| `SECRET_KEY` | 复用 `ADMIN_TOKEN` | 会话签名密钥，建议单独配置 |
| `TRUSTED_PROXIES` | `127.0.0.1,::1` | 信任哪些代理的转发头，支持网段；`*` 表示全信 |
| `LOGIN_RATE_LIMIT` | `10` | 单 IP 5 分钟内后台登录尝试上限，`0` 不限制 |
| `SOCIAL_GUARD` | `true` | 社交平台防洪总开关（后台可覆盖） |
| `SOCIAL_BLOCK_HINT` | 见 `.env.example` | 社交平台拦截页提示语 |

## 访问模式

站点级开关，决定外部访客能干什么，后台「设置 → 访问模式」随时可改。

| 模式 | 访客能创建 | 访客能看链接库/详情/二维码 | 访客能打开短链 | 访客能看统计页 |
| --- | --- | --- | --- | --- |
| `open` 开放外部可使用 | 可以 | 可以 | 可以 | 可以 |
| `admin` 仅管理员可使用 | 需登录 | 需登录 | 可以 | 需登录 |

无论哪种模式，**改、删、后台本身都要求管理员登录**。未配置 `ADMIN_TOKEN` 时自动按 `open` 运行，避免配错把站点锁死在门外。

```bash
# 仅管理员可使用
ACCESS_MODE=admin ADMIN_TOKEN=$(openssl rand -hex 24) ./run.sh
```

## 社交平台防洪（微信 / QQ 等）

短链在国内最常见的传播路径就是微信、QQ 里点开，而这两家客户端的内置浏览器
带着能认出来的 UA（`MicroMessenger`、`wxwork`、`QQ/`、`MQQBrowser`…）。
这类流量既是主要来源，也是刷量与投诉的源头，所以给了逐平台的三档策略：

| 策略 | 跳转 | 计数 | 适用 |
| --- | --- | --- | --- |
| 放行并计数 `allow` | ✅ | ✅ | 默认，正常分享 |
| 放行不计数 `silent` | ✅ | ❌ | 怀疑被刷量，又不想影响真实用户 |
| 拦截并提示 `block` | ❌ 返回中转页 | ❌（留痕） | 外链被举报、想在站外先拦一道 |

后台「防洪 / UA 屏蔽 → 社交平台防洪」按平台逐个选，改完即时生效，不用重启。
认得出的平台：微信、企业微信、QQ、微博、支付宝、钉钉、抖音、小红书。
企业微信的 UA 里同样带 `MicroMessenger`，所以先认企业微信，别把同事当外人。

被拦下的访问不会白屏，给一张带品牌的中转页：提示语可自定义，附「继续访问」
「复制链接」和 iOS/安卓都适用的三步指引——微信里外链被墙时，用户最需要的
其实是「怎么绕过去」，不是一句 403。

**这些不是靠 `User-Agent` 一刀切**：

- UA 认不出时还会看 `Referer`，从 `mp.weixin.qq.com` 点进来同样算微信来源
- 只认客户端的特征标记，不封整个 `Android` / `iPhone`，普通手机浏览器照常
- 拦截只发生在跳转入口，统计页、二维码、API 不受影响
- 被拦的访问落一条 `blocked` 事件：不计入点击，但仪表盘看得到拦了多少

统计侧多两处：链接统计页与后台都有「来源平台」分布（近 14 天 / 全部），
微信里点开多少、QQ 里点开多少、被拦多少，一屏看清。后台另有「拦截留痕」，
最近被拦的访问按平台列出来（短码、平台、时间、UA 摘要），只留痕不计数。

### 配额的两处硬账

- **每日配额按自然日重置**：`click_daily_limit` 走的是「今天的第几次」，
  不是滚动 24 小时。滚动窗口会让昨晚刷满的人在今天早上继续被关在门外。
- **限速器是原子的**：先读后写要加锁，否则并发下实际放行量能到上限的好几倍，
  热链接上尤其明显。没有客户端地址时按同一个「unknown」算配额，不是直接放行。

### 来源判定顺序

1. UA 特征标记（企业微信先于微信，它的 UA 也带 `MicroMessenger`）
2. `Referer` 里的平台域名（`mp.weixin.qq.com`、`url.cn`、`qzone.qq.com`…）
3. 都认不出就当其它来源

`Referer` 这步是必要的：微信里用「复制链接」再打开、或从聊天窗口外链进来，
UA 常常是干净的 Safari / Chrome，只看 UA 会漏掉一大半真实来源。

> 提示：UA 是客户端自报的，能伪造。这里的定位是「挡住大量真实的内置浏览器
> 流量并给真实用户一条出路」，不是对抗刻意伪装的攻击者——那种要靠限速和配额。

## 安全

### 传输与会话

- 全站响应统一带 `X-Content-Type-Options`、`X-Frame-Options`、`Referrer-Policy` 等安全头
- 页面下发带 nonce 的 CSP：脚本只放行 `'self'` 与带 nonce 的内联块，挡住注入的脚本
- 管理会话是 HMAC-SHA256 签名的 HttpOnly Cookie，`SameSite=Lax`，服务端不存会话；改 `SECRET_KEY` 等于全站踢下线
- 后台登录按 IP 限速（默认 5 分钟 10 次），配额用尽后连正确口令也挡

### CSRF

所有写请求都要在 `X-CSRF-Token` 头里回带页面下发的令牌，与 cookie 比对一致才放行。攻击者能诱导浏览器带 cookie，但读不到 cookie 内容，所以伪造不出这个头。`/yourls-api` 兼容接口与静态资源不参与校验。

### 口令哈希

`ADMIN_TOKEN` 支持直接填哈希值而不必存明文，算法优先 Argon2id（m=19MiB / t=2 / p=1），缺依赖时自动退到 PBKDF2-HMAC-SHA256 60 万次迭代。哈希编码带算法名前缀，参数低于当前标准时会提示升级。

```bash
python -c "from app.core.crypto import hash_secret; print(hash_secret('your-password'))"
```

### 客户端 IP

默认只信任回环代理的 `X-Forwarded-For` / `X-Real-IP`。不设这道关的话，任何人都能伪造转发头把限速和每日配额绕得一干二净。上了 Nginx 就把内网网段填进 `TRUSTED_PROXIES`。

### URL 校验

创建短链时挡掉带 userinfo 的地址（`https://trusted.com@evil.com` 这类钓鱼伪装）、内网 / 回环 / 链路本地 / IPv4 映射 IPv6 地址，以及非 http/https 协议。自动取标题的请求同样有 SSRF 防护。

## Logo

位于 `app/static/logo/`，全部**无背景透明**：

- `icon.svg` / `icon-{16,32,48,64,96,128,180,192,256,512}.png` — 主图标
- `favicon.svg` / `../favicon.ico` — 站点图标
- `icon-mono.svg` — 单色版，用 `currentColor` 即可换色
- `logo-horizontal.svg` — 图标 + 字标横版

重新生成：

```bash
python scripts/gen_logo.py
```

## 测试

```bash
pip install -r requirements-dev.txt
pytest
```

198 个用例覆盖创建校验、跳转、停用/过期、列表筛选排序、更新删除、统计、设备分布、编辑与 CSV 导出、访问模式、管理后台、YOURLS 迁移、社交平台防洪（平台识别含 Referer 兜底、三档策略含 `silent` 落地、拦截页、总开关、来源统计、拦截留痕），以及安全加固（响应头、CSP、CSRF、可信代理、URL 校验、登录限速、口令哈希）与限速器本身（自然日重置、并发不上限、0 视为不限）。

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
├── core/              config / db / codes / security / crypto / csrf
│                       middleware / access / admin / apikey / guard
│                       clicks / settings_store / titles / yourls / timeutil
│                       social（微信 / QQ 等来源识别与策略）
├── templates/         Jinja2 模板
├── static/            CSS / JS / Logo
├── tests/             pytest 用例
│   ├── test_api.py          创建/跳转/筛选/更新/删除
│   ├── test_new.py          编辑/导出/设备统计/UA/访问令牌
│   ├── test_v3.py           后台/延迟计数/防洪/YOURLS 迁移
│   ├── test_access_mode.py  访问模式
│   ├── test_social.py       社交平台防洪
│   └── test_security.py     安全加固
└── scripts/           Logo 生成脚本
```

## 说明

- 默认 SQLite，日活十万以内完全够用；量大了切 Postgres 只需改 `DATABASE_URL`
- 统计数据仅存 IP 的 SHA-256 前 32 位，不落明文 IP
- 停用 / 过期 / 不存在的短码都会返回友好页面，不会裸奔报错
- 延迟计数是内存缓冲，进程退出时会强制结算一次；极端崩溃可能丢最近几秒的点击
- 被 UA 屏蔽的访问仍会跳转，也会留一条带 `blocked` 标记的记录，只是不计入点击数
- 自动取标题会请求目标站，已做 SSRF 防护（内网/回环/链路本地地址一律不抓）
