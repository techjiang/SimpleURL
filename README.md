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
- **设备统计** — 桌面端 / 移动端 / 机器人来源一目了然
- **可选私密** — 配一个 `ACCESS_TOKEN` 就能把整个站点锁起来
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

55 个用例覆盖创建校验、跳转、停用/过期、列表筛选排序、更新删除、统计、设备分布、编辑与 CSV 导出。

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
├── core/              config / db / codes / security / timeutil
├── templates/         Jinja2 模板
├── static/            CSS / JS / Logo
├── tests/             pytest 用例
│   ├── test_api.py      创建/跳转/筛选/更新/删除
│   └── test_new.py      编辑/导出/设备统计/UA/访问令牌
└── scripts/           Logo 生成脚本
```

## 说明

- 默认 SQLite，日活十万以内完全够用；量大了切 Postgres 只需改 `DATABASE_URL`
- 统计数据仅存 IP 的 SHA-256 前 32 位，不落明文 IP
- 停用 / 过期 / 不存在的短码都会返回友好页面，不会裸奔报错
- 延迟计数是内存缓冲，进程退出时会强制结算一次；极端崩溃可能丢最近几秒的点击
- 被 UA 屏蔽的访问仍会跳转，也会留一条带 `blocked` 标记的记录，只是不计入点击数
- 自动取标题会请求目标站，已做 SSRF 防护（内网/回环/链路本地地址一律不抓）
