# SimpleURL

> 轻量、流畅、好用的高级短链接系统。粘贴链接，回车即得。

自托管、单文件部署、自带访问统计与二维码。技术栈：**FastAPI + SQLAlchemy + SQLite + 原生前端**，无构建步骤。

## 特性

- **一键生成** — 粘贴长链接回车即得短码，也可自定义短码
- **访问模式** — 开放外部可使用 / 仅管理员可使用 / 邀请码可使用，一键切换
- **安全可靠** — Argon2id 密钥哈希、CSP nonce、全套安全响应头、SSRF 防护、防刷限额
- **访问统计** — 总点击 / 独立访客 / 30 天趋势图 / 来源 Top，按 IP 哈希脱敏
- **二维码** — 每条链接自动生成 PNG 二维码
- **标签管理** — 标签检索、状态筛选、多字段搜索、多种排序
- **链接控制** — 随时停用 / 启用 / 删除，支持设置过期时间
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
| `GET` | `/api/qr/{code}` | 二维码 PNG |
| `GET` | `/api/stats/summary` | 总览 |
| `GET` | `/{code}` | 跳转（307），自动记录点击 |
| `GET` | `/api/access` | 当前访问模式与身份 |
| `POST` | `/api/admin/login` | 管理员登录（`X-Admin-Token` 头） |
| `POST` | `/api/admin/logout` | 退出登录 |
| `GET` | `/api/admin/invite` | 查看当前邀请码（管理员） |
| `POST` | `/api/invite?code=` | 用邀请码解锁创建 |

> 列表、详情、更新、删除、统计均为管理员接口；未登录时返回 401。
> 创建接口受访问模式约束；跳转始终对所有人开放。

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
| `DEBUG` | `false` | 开启后开放 `/api/docs` |
| `ACCESS_MODE` | `public` | 访问模式：`public` / `admin` / `invite` |
| `ADMIN_TOKEN` | 空 | 管理密钥，留空则管理功能关闭；可直接填 Argon2id 哈希 |
| `ADMIN_ONLY` | `false` | 旧版开关，等价于 `ACCESS_MODE=admin` |
| `SECRET_KEY` | 随机 | 会话签名密钥，与 `ADMIN_TOKEN` 无关；改它等于全站重新登录 |
| `CSP_REPORT_ONLY` | `false` | CSP 只上报不拦截，上线初期用于观察误伤 |
| `TRUSTED_PROXIES` | 空 | 可信反代地址，逗号分隔；留空则不采信 `X-Forwarded-For` |

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

102 个用例覆盖创建校验、跳转、停用/过期、列表筛选排序、更新删除、统计与二维码，
三种访问模式、登录登出与鉴权绕过，以及安全加固专项：响应头、CSP nonce、
Argon2id 哈希与降级、会话绑定与轮换、Cookie 属性、IP 信任边界、开放跳板。

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
├── core/              config / db / codes / crypto / security / auth / middleware / timeutil
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

### 其它

- URL 校验拦掉内网 / 回环 / 链路本地地址，自动取标题时同样生效（防 SSRF）
- 短码目标强制补成完整绝对 URL，危险协议（`javascript:`、`data:`、`file:`）一律拒绝
- 统计数据只存 IP 的 SHA-256 前 32 位，不落明文
- 停用 / 过期 / 不存在的短码都给友好页面，不裸奔 JSON
- 未配 `ADMIN_TOKEN` 时 `admin` / `invite` 自动退回 `public`，
  避免少写一行配置把整站锁死
