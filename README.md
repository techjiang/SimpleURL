# SimpleURL

> 轻量、流畅、好用的高级短链接系统。粘贴链接，回车即得。

自托管、单文件部署、自带访问统计与二维码。技术栈：**FastAPI + SQLAlchemy + SQLite + 原生前端**，无构建步骤。

## 特性

- **一键生成** — 粘贴长链接回车即得短码，也可自定义短码
- **安全可靠** — URL 白名单校验、内网地址拦截、短码去重、防刷创建限额
- **访问统计** — 总点击 / 独立访客 / 30 天趋势图 / 来源 Top，按 IP 哈希脱敏
- **二维码** — 每条链接自动生成 PNG 二维码
- **标签管理** — 标签下拉直接用库里的真实标签，前后台都能按标签筛，状态筛选与多字段搜索并存
- **链接控制** — 随时停用 / 启用 / 删除 / 编辑，支持设置或清除过期时间
- **批量操作** — 勾选多条一次性启用 / 停用 / 删除，翻页仍保留已选（上限 500 条）
- **操作留痕** — 创建、修改、删除、改设置、导入都记一条，谁在什么时候动了哪条一目了然
- **数据导出** — 按当前筛选一键导出 CSV，Excel 直接打开不乱码
- **社交平台防洪** — 微信 / QQ / 微博等内置浏览器逐平台放行、静默或拦截，带中转页与来源分布
- **自动取标题** — 创建时真去抓目标页 `<title>`（重定向逐跳过 SSRF 闸），抓不到用 URL 兜底，并记录来源：抓取 / 兜底 / 手填
- **设备统计** — 桌面端 / 移动端 / 机器人来源一目了然
- **可选私密** — 配一个 `ACCESS_TOKEN` 就能把整个站点锁起来
- **安全加固** — CSP、CSRF 防护、Argon2id 口令哈希、登录限速、可信代理
- **到期提醒** — 列表与仪表盘都标出「还剩几天」，7 天内到期可一键筛出
- **运行状态** — 后台一屏看库体积 / 待结算 / 丢了多少点击 / 部署自检告警
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
| `GET` | `/api/links/{code}/stats` | 访问统计（管理员，或创建者带 `?t=` 回执） |
| `GET` | `/api/qr/{code}` | 二维码 PNG（管理员，或创建者带 `?t=` 回执） |
| `GET` | `/api/links/export` | 导出 CSV（沿用列表的筛选参数） |
| `POST` | `/api/links/bulk` | 批量操作，`action` 取 `enable`/`disable`/`delete`，`codes` 为短码数组 |
| `GET` | `/api/stats/summary` | 总览（含站点公告与访问令牌状态） |
| `GET` | `/api/access` | 当前访问模式与登录态 |
| `POST` | `/api/admin/login` | 后台登录 |
| `GET` | `/api/admin/dashboard` | 仪表盘数据 |
| `GET/POST` | `/api/admin/settings` | 读取 / 保存运行参数 |
| `GET/POST` | `/api/admin/ua-rules` | UA 屏蔽规则 |
| `GET/POST/DELETE` | `/api/admin/api-keys` | API 密钥管理 |
| `POST` | `/api/admin/migrate/yourls` | 从 YOURLS 迁移 |
| `GET` | `/api/admin/audit` | 操作留痕，支持 `limit` / `action` |
| `POST` | `/api/admin/flush-clicks` | 立即结算延迟计数 |
| `GET` | `/api/admin/runtime` | 运行状态：库体积、待结算、丢弃数、到期数、部署自检 |
| `POST` | `/api/admin/prune-events` | 清理保留期外的点击明细（累计点击数不变） |
| `POST` | `/yourls-api` | YOURLS 兼容接口（shorturl / expand / url-stats / db-stats） |
| `GET` | `/{code}` | 跳转（307），延迟记录点击 |
| `GET` | `/healthz` | 健康检查，带待结算与丢弃计数 |

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
| `CODE_LENGTH` | `4` | 自动生成短码长度（3~16，后台可改） |
| `CODE_NO_AMBIGUOUS` | `true` | 随机码排除易混字符 `0 O 1 l I` |
| `CODE_MIN_LENGTH` / `CODE_MAX_LENGTH` | `3` / `16` | 自定义短码长度限制 |
| `CREATE_DAILY_LIMIT` | `200` | 单 IP 每日创建上限（自然日重置），后台可改，`0` 不限制 |
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
| `SSRF_RESOLVE_CHECK` | `true` | 创建短链时解析域名核对内网地址；关掉只校验字面 IP |
| `DNS_CACHE_TTL` | `60` | 解析结果缓存秒数，避免同一域名反复查 DNS |
| `CLICK_BUFFER_MAX` | `20000` | 延迟计数队列上限，超限丢弃最旧的并计数 |
| `CLICK_EVENT_RETENTION_DAYS` | `365` | 点击明细保留天数，`0` 不清理（累计点击数不受影响） |
| `ANNOUNCEMENT` | 空 | 首页顶部公告，后台「设置 → 站点公告」可改 |
| `SITE_TAGLINE` | `数据可自托管，隐私尽在掌握` | 页脚那句自述 |

## 访问模式

站点级开关，决定外部访客能干什么，后台「设置 → 访问模式」随时可改。

| 模式 | 访客能创建 | 访客能看链接库 / 详情 / 导出 / 统计 | 访客能打开短链 | 自己刚建的那条 |
| --- | --- | --- | --- | --- |
| `open` 开放外部可使用 | 可以 | 不可以（仅管理员） | 可以 | 凭回执链接可看 |
| `admin` 仅管理员可使用 | 需登录 | 不可以（仅管理员） | 可以 | 无 |

两条硬规则：

- **跳转永远对所有人开放。** 短链的命根子是「点得开」，模式只管「能不能建」。
- **链接库与统计永远只给管理员。** 开放模式开的是「建」，不是「看别人的」——
  一份完整的链接清单摊在公网上，等于把所有人建的短链都曝出来。

未配置 `ADMIN_TOKEN` 时自动按 `open` 运行，避免配错把站点锁死在门外；
配了但短于 12 位则按「仅管理员」收权（见下方「口令强度」）。

### 回执链接：访客也能看自己那条

开放模式下访客建完链接，响应里会多一条 **回执链接**（`/s/<短码>?t=<领取码>`）。
凭它能看到这一条的详情、统计与二维码；换个短码就 401，删掉领取码即失效。

- 领取码只返回一次，服务端只存哈希；它**不是第二套账号**，只是一次性回执
- 管理员自己创建时不发领取码（用不着）
- 回执会一并带进统计页里那张二维码的地址，不然那张图会被挡在门外

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

### 操作留痕

短链接最典型的事故不是宕机，是「我的链接怎么跳到别处去了」。没有留痕只能靠猜，
所以所有写操作（创建 / 修改 / 删除 / 批量 / 改设置 / 导入 / 登录）都落一条
`audit_logs`，后台「操作留痕」按时间倒序看，可按动作筛选。

只留动作与对象：操作者、短码、变更字段、时间。IP 与点击统计一样只存哈希，
日志表只保留最近 2000 条，长跑进程不能只涨不落。

### URL 校验

创建短链时挡掉带 userinfo 的地址（`https://trusted.com@evil.com` 这类钓鱼伪装）、内网 / 回环 / 链路本地 / IPv4 映射 IPv6 地址，以及非 http/https 协议。

只校验字面 IP 是不够的：`http://10.0.0.1.nip.io` 这类域名自带解析，前一秒还是「一个普通域名」，
后端一请求就摸到内网。所以域名会再看一眼解析结果，任一地址落在内网就拒收，
解析失败也按不安全处理（宁可少收一个域名，不可放行一次内网探测）。
结果缓存 `DNS_CACHE_TTL` 秒——不能长，DNS 重绑定要的正是「第一次解析是公网」。

自动取标题走的是同一条判定（`titles._is_safe_host` 直接复用 `is_public_url`），
两套规则各写一遍，早晚会有一边漏。

### 口令强度

`ADMIN_TOKEN` 短于 12 位时，访客能做的事一律收回（创建、链接库、统计全关），
只保留跳转；后台仍可登录去换口令，但「密码能猜到」不会再顺带变成「别人能当管理员」。
后台「运行状态 → 部署自检」会把这条和其它配置隐患直接列出来。

### 访问令牌

开了 `ACCESS_TOKEN` 之后，`?token=xxx` 校验通过就写进 HttpOnly Cookie，
之后刷新、翻页、看统计页里那张二维码都不用再带参数。前端门禁存的是
sessionStorage 里的明文，靠拼 query 跳转——服务端不认它，门禁就是死循环。

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

276 个用例覆盖创建校验、跳转、停用/过期、列表筛选排序、更新删除、统计、设备分布、编辑与 CSV 导出、访问模式、管理后台、YOURLS 迁移、社交平台防洪（平台识别含 Referer 兜底、三档策略含 `silent` 落地、拦截页、总开关、来源统计、拦截留痕），以及安全加固（响应头、CSP、CSRF、可信代理、URL 校验、登录限速、口令哈希）与限速器本身（自然日重置、并发不上限、0 视为不限），以及回执链接、访客准入边界、短码长度可调、本地时区显示，以及 v5 的批量操作、
操作留痕、创建额度按后台配置生效、跳转策略快照（改配置立刻生效），
以及 v6 的域名解析级 SSRF 拦截（含 DNS 缓存与取标题侧）、延迟计数队列上限、
根级图标与 HEAD 请求路由、访问令牌 Cookie 落盘、到期提醒筛选、运行状态与点击明细清理、弱口令收权、站点公告。

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
│                       policy（跳转策略快照）/ report（操作留痕）
│                       social（微信 / QQ 等来源识别与策略）
├── templates/         Jinja2 模板
├── static/            CSS / JS / Logo
├── tests/             pytest 用例
│   ├── test_api.py          创建/跳转/筛选/更新/删除
│   ├── test_new.py          编辑/导出/设备统计/UA/访问令牌
│   ├── test_v3.py           后台/延迟计数/防洪/YOURLS 迁移
│   ├── test_access_mode.py  访问模式
│   ├── test_social.py       社交平台防洪
│   ├── test_v4.py           回执链接 / 准入边界 / 短码长度
│   ├── test_v5.py           批量操作 / 操作留痕 / 额度 / 策略快照
│   └── test_security.py     安全加固
└── scripts/           Logo 生成脚本
```

## 说明

- 默认 SQLite，日活十万以内完全够用；量大了切 Postgres 只需改 `DATABASE_URL`
- 统计数据仅存 IP 的 SHA-256 前 32 位，不落明文 IP
- 停用 / 过期 / 不存在的短码都会返回友好页面，不会裸奔报错
- 延迟计数是内存缓冲，进程退出时会强制结算一次；极端崩溃可能丢最近几秒的点击
- 跳转链路上的防洪策略（频率上限 / 社交开关 / 平台策略 / UA 规则）合并成 3 秒快照，
  减少热链接上的重复查询；后台一保存立刻失效，改完马上生效
- 被 UA 屏蔽的访问仍会跳转，也会留一条带 `blocked` 标记的记录，只是不计入点击数
- 自动取标题会请求目标站，已做 SSRF 防护（内网/回环/链路本地地址一律不抓；
  重定向逐跳校验，防止公网地址 302 到内网元数据接口）。它在独立线程里跑，
  不会撞上「事件循环已在运行」；超时上限由 `FETCH_TITLE_TIMEOUT` 控制，
  不想等出网就在创建请求里带 `fetch_title: false`
- **站内静态资源允许被外站引用**：短链站的核心用途就是贴到别处，微信 / QQ 抓预览卡片、
  博客里嵌二维码，都是跨站引用。默认 `STATIC_CROSS_ORIGIN=true`，
  只对 `/static/`、图标和 `/api/qr/` 放开 `Cross-Origin-Resource-Policy`；
  页面本身仍是 `same-origin` + `X-Frame-Options: DENY`，防点击劫持不受影响
