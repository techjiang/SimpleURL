#!/usr/bin/env bash
# SimpleURL 一键安装 / 运维脚本
#
#   交互安装   curl -fsSL https://cnb.cool/<仓库>/-/raw/main/install.sh | bash -s -- --yes
#   本地安装   ./install.sh                 问几个问题，其余全自动
#   全默认     ./install.sh --yes           不问，密码随机生成
#   只做诊断   ./install.sh doctor          不写任何文件，先看环境行不行
#   升级       ./install.sh upgrade         拉新代码，失败自动回滚
#   卸载       ./install.sh uninstall
#
# 设计上的几条硬规矩：
#   · 装完的目录能自己管自己：cp install.sh 到目录里就能修，不依赖 /usr/local/bin
#   · 升级失败必须能回去：先备份数据和 .env，构建或启动不成就回滚
#   · 不许悄悄降级：远端版本比本地旧时不再覆盖代码
#   · 每写一步都有自检：Python 版本、端口、磁盘、健康检查，问题当场说清楚
set -euo pipefail

VERSION="2.7.0"
# 本地脚本所在目录。`curl | bash` 时为空，只能靠下载源码包。
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo "")"
SELF_PATH="${BASH_SOURCE[0]:-install.sh}"

# ---------------------------------------------------------------- 常量
# 默认从本项目仓库安装；换镜像源只改这两个变量
GIT_HOST="${GIT_HOST:-https://cnb.cool}"
REPO_SLUG="${REPO_SLUG:-broccoli_xi/TechSauce/SimpleURL}"
REPO_BRANCH="${REPO_BRANCH:-main}"
REPO_URL="${REPO_URL:-$GIT_HOST/$REPO_SLUG.git}"
# 平台 OpenAPI 域名：CNB 的网页路由（/-/raw、/-/archive）是前端页面，
# 直接 curl 拿到的是 HTML 外壳，所以版本探测和源码包都优先走 API。
API_HOST="${API_HOST:-https://api.cnb.cool}"
# 源码包地址：CNB 走 OpenAPI 的 archive 接口；其它平台用各自的归档地址
default_tarball() {
  # 平台按 GIT_HOST / API_HOST / REPO_URL 认：换镜像、走自建源时可能只有其中一个变，
  # 只看 GIT_HOST 会把 CNB 的路径规则（/-/git/archive、/-/git/raw）整段跳过。
  case "$GIT_HOST $API_HOST $REPO_URL" in
    *cnb.cool*|*"$REPO_SLUG"*)
      # 有令牌走 OpenAPI（私库也能拉）；没令牌走网页的 /-/git/archive ——
      # 它返回的也是真 tar.gz（对过魔数），而 /-/archive 是前端页面，只有 HTML 外壳。
      if [ -n "${CNB_TOKEN:-}" ]; then
        printf '%s/%s/-/git/archive/%s.tar.gz' "$API_HOST" "$REPO_SLUG" "$REPO_BRANCH"
      else
        printf '%s/%s/-/git/archive/%s.tar.gz' "$GIT_HOST" "$REPO_SLUG" "$REPO_BRANCH"
      fi ;;
    *)
      printf '%s/%s/-/archive/%s.tar.gz' "$GIT_HOST" "$REPO_SLUG" "$REPO_BRANCH" ;;
  esac
}
REPO_TARBALL="${REPO_TARBALL:-$(default_tarball)}"
# 源码加速前缀：只对 GitHub 有用。CNB / Gitee 本身在国内，
# 再套一层加速反而多一跳（实测还会 403），所以按域名自动判断要不要用。
GIT_MIRROR="${GIT_MIRROR:-https://ghfast.top/}"

# 安装目录：显式 SIMPLEURL_HOME / --dir 优先；其次是「脚本自己就躺在安装目录里」
# （./install.sh upgrade 这种最自然的用法，不该还要人再设一次变量）
dir_from_args() { # args...
  local prev="" a
  for a in ${1+"$@"}; do
    if [ "$prev" = "--dir" ] && [ -n "$a" ]; then printf '%s' "$a"; return 0; fi
    prev="$a"
  done
}

# 安装目录规范化。不这么做，同一个目录用不同写法就会变成「两份安装」：
#   · `bash install.sh --dir ./site` 在 install.sh 眼里是 ./site，在生成的
#     simpleurl 眼里（DIR 由它自己 cd 到物理路径）是 /abs/site —— 于是自己的
#     目录里有命令却认不出来（退回 install.sh 硬顶），卸载时也把两者当成两个目录。
#   · /tmp/su1-link 这种软链更糟：/tmp/su1-link 与 /tmp/su1 各自有标记文件，
#     app/ 被复制到软链深处（.venv/bin/python 的软链当场失效），代码版本却从
#     标记文件读 —— 屏幕上说「代码未变动」，实际装进去的可能是另一份分支。
# 先按物理路径（pwd -P）解析，再用它拼出所有派生路径。
#
# LOG_FILE / MARK_FILE 也是从 INSTALL_DIR 派生的常量，而 --dir 在参数解析时才读到。
# 早先只改 INSTALL_DIR、没重算这两个，结果拿到 --dir 的安装把日志往
# $HOME/simpleurl 写 —— 目录不存在，nohup 当场失败，报一句跟真正原因无关的错。
_abs_dir() { # path
  case "$1" in
    /*) printf '%s' "$1" ;;
    *)  printf '%s/%s' "$(pwd)" "$1" ;;
  esac
}
_canon_dir() { # path
  local p; p="$(_abs_dir "$1")"
  ( cd "$p" 2>/dev/null && pwd -P ) || printf '%s' "$p"
}

CLI_DIR="$(dir_from_args "$@")"
SIMPLEURL_HOME="${SIMPLEURL_HOME:-$CLI_DIR}"
if [ -n "${SIMPLEURL_HOME:-}" ]; then
  INSTALL_DIR="$(_canon_dir "$SIMPLEURL_HOME")"
elif [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/.simpleurl-managed" ]; then
  INSTALL_DIR="$(_canon_dir "$SELF_DIR")"
else
  # 第三条路：本机已经装过（CLI 里记着目录）。从任意位置跑 `bash install.sh upgrade`
  # 是最自然的用法，不该因为「脚本不在安装目录里」就跑去 $HOME/simpleurl 报
  # 「没有安装记录」。装了多份时以最近一次安装为准。
  INSTALL_DIR="$HOME/simpleurl"
  # 先看全局命令（两份安装时以最近一次为准），再扫常见位置下的影子 ——
  # 全局命令被后装的那份覆盖后，影子是唯一还能认出前一份目录的线索。
  for _mark in /usr/local/bin/simpleurl "$HOME/.local/bin/simpleurl" \
               "$HOME"/simpleurl/simpleurl "$HOME"/*/simpleurl/simpleurl; do
    [ -f "$_mark" ] || continue
    _dir="$(sed -n 's/^DIR="\(.*\)"$/\1/p' "$_mark" 2>/dev/null | head -1)"
    if [ -n "$_dir" ] && [ -f "$_dir/.simpleurl-managed" ]; then INSTALL_DIR="$(_canon_dir "$_dir")"; break; fi
  done
  unset _mark _dir
fi

# 8000 是各种开发服务器的默认口，撞车概率太高；8940 冷门、好记、不占常用段
PORT="${PORT:-8940}"
SERVICE_NAME="${SERVICE_NAME:-simpleurl}"
SITE_NAME="${SITE_NAME:-SimpleURL}"
USE_CN_MIRROR="${USE_CN_MIRROR:-auto}"     # auto | 1 | 0
PIP_MIRRORS="${PIP_MIRRORS:-https://pypi.tuna.tsinghua.edu.cn/simple https://mirrors.aliyun.com/pypi/simple https://mirrors.cloud.tencent.com/pypi/simple https://mirrors.ustc.edu.cn/pypi/simple}"
DOCKER_MIRRORS="${DOCKER_MIRRORS:-https://docker.1ms.run,https://docker.m.daocloud.io,https://docker.xuanyuan.me,https://dockerproxy.net}"
COMPOSE_FILE_NAME="docker-compose.yml"
LOG_FILE="$INSTALL_DIR/simpleurl.log"
MARK_FILE="$INSTALL_DIR/.simpleurl-managed"   # 有它才认作本脚本装的目录（内含运行方式等）
# 这台机器上还装着哪些 SimpleURL。两份安装共用一个命令名，后装的那份会把
# /usr/local/bin/simpleurl 覆盖成自己的；卸掉它时得知道「先装那份在哪」，
# 否则命令被删掉、站点还在跑，人以为装的东西凭空没了。
SIBLING_FILE="$INSTALL_DIR/.simpleurl-siblings"

RUNTIME=""          # docker | python，安装时定，simpleurl 命令会读它
SOURCE_REUSED=""    # y 表示这轮没换代码，只重装了依赖 / 重启了服务
ACTION_UPGRADE="n"  # 由 `upgrade` 子命令置 y，用来走「版本没变就别折腾」的短路
BANNER_SHOWN="n"
PROXY_FILE=""       # 生成的反向代理配置文件路径
PROXY_KIND=""       # nginx | caddy | apache
RUNTIME_OVERRIDE="" # --runtime docker|python
# 回滚点。install_source 在换代码前填 PREV_BACKUP，install_docker_mode 在换镜像前填
# PREV_IMAGE；必须在这里先声明——放到文件后半段，`set -u` 下先用后赋值就是 unbound variable。
PREV_IMAGE=""
PREV_BACKUP=""
INTERACTIVE=1
ACTION="install"
SUB_ARGS=()          # 转发给 simpleurl 的额外参数
ASSUME_YES="n"
SKIP_HEALTH="n"
STRICT="n"           # doctor --strict：把「提醒」也算成阻塞
PORT_EXPLICIT="n"    # 端口是用户显式给的（--port / PORT=）还是默认值
[ -n "${PORT:-}" ] && PORT_EXPLICIT="y"

while [ $# -gt 0 ]; do
  case "$1" in
    install)     ACTION="install" ;;
    upgrade|update) ACTION="upgrade" ;;
    --yes|-y)    INTERACTIVE=0 ;;
    uninstall|remove) ACTION="uninstall"; INTERACTIVE=0 ;;
    doctor|check) ACTION="doctor" ;;
    --runtime)   shift; RUNTIME_OVERRIDE="${1:-}" ;;
    --branch)    shift; REPO_BRANCH="${1:-main}"; REPO_TARBALL="$(default_tarball)" ;;
    --port)      shift; PORT="${1:-$PORT}"; PORT_EXPLICIT="y" ;;
    # --dir 要同时改写 SIMPLEURL_HOME：install_cli / 卸载 / 升级都从它算安装目录，
    # 只改 INSTALL_DIR 会让后面几步偷偷回到 $HOME/simpleurl
    # --dir 同样规范化：./site、/tmp/link-to-site、~/site 都收敛到同一个物理路径，
    # 否则「同一份安装」在脚本和 simpleurl 命令眼里是两个目录
    --dir)       shift; SIMPLEURL_HOME="${1:-$SIMPLEURL_HOME}"
                 INSTALL_DIR="$(_canon_dir "$SIMPLEURL_HOME")"; SIMPLEURL_HOME="$INSTALL_DIR"
                 LOG_FILE="$INSTALL_DIR/simpleurl.log"; MARK_FILE="$INSTALL_DIR/.simpleurl-managed" ;;
    --no-health) SKIP_HEALTH="y" ;;
    --strict)    STRICT="y" ;;
    --force)     ASSUME_YES="y" ;;
    --help|-h)   ACTION="help" ;;
    --version|-V) ACTION="version" ;;
    # 帮助里列出的管理子命令：脚本自己不重复实现一遍，转给安装时生成的
    # simpleurl 命令。以前这些词会直接落到「未知参数」上 —— 帮助里写得
    # 明明白白，照着敲却全报错，是最伤信任的那种不一致。
    status|start|stop|restart|log|url|password|config|backup|restore|version|upgrade|doctor)
                 ACTION="$1"
                 shift
                 SUB_ARGS=("$@")   # 解析循环会把 $@ 消耗光，这里先留一份
                 break ;;
    *) echo "未知参数：$1（--help 看用法）" >&2; exit 2 ;;
  esac
  shift
done

# ---------------------------------------------------------------- 输出
if [ -t 1 ]; then
  RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; BLUE=$'\033[36m'
  BOLD=$'\033[1m'; DIM=$'\033[2m'; RST=$'\033[0m'
else
  RED=""; GREEN=""; YELLOW=""; BLUE=""; BOLD=""; DIM=""; RST=""
fi
step() { printf "%s▸%s %s\n" "$BLUE" "$RST" "$*"; }
ok()   { printf "%s✓%s %s\n" "$GREEN" "$RST" "$*"; }
warn() { printf "%s!%s %s\n" "$YELLOW" "$RST" "$*"; }
die()  { printf "%s✗%s %s\n" "$RED" "$RST" "$*" >&2; exit 1; }
hr()   { printf "%s──────────────────────────────────────────────%s\n" "$DIM" "$RST"; }
need_cmd() { command -v "$1" >/dev/null 2>&1; }

banner() {
  printf "\n%s%sSimpleURL%s %s· 轻量、流畅、好用的短链接系统 %s\n" \
    "$GREEN" "$BOLD" "$RST" "$DIM" "$RST"
  printf "%s一个链接，短到可以记住。%s\n\n" "$DIM" "$RST"
}

usage() {
  cat <<EOS
SimpleURL 安装脚本 v$VERSION

  ./install.sh                交互安装（问站点名、端口、域名、账号密码）
  ./install.sh --yes          全默认安装，密码自动生成
  ./install.sh doctor         只做环境体检，不装任何东西
  ./install.sh upgrade        更新到最新代码（失败自动回滚）
  ./install.sh uninstall      卸载（会二次确认）
  ./install.sh --version      打印脚本版本
  ./install.sh --help         这段说明

  可选参数：--port 8000  --dir ~/simpleurl  --runtime docker|python  --no-health
  doctor 加 --strict：连「提醒」也算不通过（CI 用）

环境变量（都可选）：
  SIMPLEURL_HOME   安装目录，默认 ~/simpleurl
  PORT             监听端口，默认 8940
  SITE_NAME        站点名称
  USE_CN_MIRROR    auto（默认）/ 1 强制国内加速 / 0 用官方源
  PIP_INDEX_URL    指定 pip 源
  DOCKER_MIRRORS   逗号分隔的 Docker 镜像加速地址
  REPO_URL / REPO_TARBALL / GIT_HOST / REPO_SLUG   换安装来源

装完用 simpleurl 管理（或 ./install.sh <子命令>）：
  status | start | stop | restart | log | url | password
  config | backup | restore | upgrade | doctor | uninstall
EOS
}

# 交互读取一律走 /dev/tty：curl | bash 时 stdin 是脚本本身，read 读不到键盘。
# 没有终端（CI、面板、cron）就安静地用默认值，别刷一堆报错。
if [ -r /dev/tty ] && { : < /dev/tty; } 2>/dev/null; then TTY=/dev/tty; else TTY=""; fi

ask() {
  local prompt="$1" default="${2:-}" answer=""
  if [ "$INTERACTIVE" = "0" ] || [ -z "$TTY" ]; then ANSWER="$default"; return 0; fi
  if [ -n "$default" ]; then
    printf "%s%s%s %s[%s]%s " "$BOLD" "$prompt" "$RST" "$DIM" "$default" "$RST"
  else
    printf "%s%s%s " "$BOLD" "$prompt" "$RST"
  fi
  IFS= read -r answer < "$TTY" || answer=""
  ANSWER="${answer:-$default}"
}

ask_password() {
  local prompt="$1" answer=""
  if [ "$INTERACTIVE" = "0" ] || [ -z "$TTY" ]; then ANSWER=""; return 0; fi
  printf "%s%s%s %s（留空自动生成）%s " "$BOLD" "$prompt" "$RST" "$DIM" "$RST"
  stty -echo < "$TTY" 2>/dev/null || true
  IFS= read -r answer < "$TTY" || answer=""
  stty echo < "$TTY" 2>/dev/null || true
  printf "\n"
  ANSWER="$answer"
}

ask_yes() {
  local prompt="$1" default="${2:-y}" answer=""
  if [ -n "$ASSUME_YES" ] && [ "$ASSUME_YES" = "y" ]; then ANSWER="y"; return 0; fi
  if [ "$INTERACTIVE" = "0" ] || [ -z "$TTY" ]; then ANSWER="$default"; return 0; fi
  printf "%s%s%s %s[y/n，默认 %s]%s " "$BOLD" "$prompt" "$RST" "$DIM" "$default" "$RST"
  IFS= read -r answer < "$TTY" || answer=""
  ANSWER="${answer:-$default}"
}

gen_password() {
  # 去掉容易看错的 0/O/1/l/I，手抄密码时不至于错一位
  local raw
  if need_cmd openssl; then raw="$(openssl rand -base64 32)"; else raw="$(head -c 48 /dev/urandom | base64)"; fi
  printf '%s' "$raw" | tr -dc 'A-Za-z2-9' | cut -c1-16
}
gen_key() {
  if need_cmd openssl; then openssl rand -hex "${1:-32}"
  else od -An -tx1 -N"${1:-32}" /dev/urandom | tr -d ' \n'; fi
}

detect_ip() {
  local ip=""
  ip="$(hostname -I 2>/dev/null | awk '{print $1}')" || ip=""
  [ -n "$ip" ] || ip="$(ip route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')" || true
  printf '%s' "${ip:-127.0.0.1}"
}

is_private_ip() { # ip
  case "$1" in
    10.*|192.168.*|127.*|172.1[6-9].*|172.2[0-9].*|172.3[01].*) return 0 ;;
    *) return 1 ;;
  esac
}

# ---------------------------------------------------------------- 网络与镜像
CN_MIRROR_ON=0
PIP_INDEX_URL=""
PIP_TRUSTED_HOST=""

# 真拉一个包试试。只探索引页会看走眼：索引能开、包地址 403 的源
# 照样「探测通过」，然后卡在装依赖那一步，白等几分钟。
pip_mirror_works() { # base host
  local base="$1" host="${2:-}" out rc
  out="$(mktemp 2>/dev/null || echo "/tmp/.su-probe.$$")"
  if [ -n "$host" ]; then
    python3 -m pip download --no-deps --no-cache-dir -q -d "$out" \
      -i "$base" --trusted-host "$host" six >/dev/null 2>&1
  else
    python3 -m pip download --no-deps --no-cache-dir -q -d "$out" -i "$base" six >/dev/null 2>&1
  fi
  rc=$?
  rm -rf "$out"
  return $rc
}

pick_pip_mirror() {
  local base host
  for base in $PIP_MIRRORS; do
    case "$base" in
      *aliyun*)  host="mirrors.aliyun.com" ;;
      *tencent*) host="mirrors.cloud.tencent.com" ;;
      *ustc*)    host="mirrors.ustc.edu.cn" ;;
      *tuna*)    host="pypi.tuna.tsinghua.edu.cn" ;;
      *)         host="" ;;
    esac
    # 只探首页，3 秒超时；不通就换下一个，绝不卡死
    if curl -fsS -m 3 -o /dev/null "$base" 2>/dev/null \
       && pip_mirror_works "$base" "$host"; then
      PIP_INDEX_URL="$base"; PIP_TRUSTED_HOST="$host"; return 0
    fi
  done
  return 1
}

probe_network() {
  case "$USE_CN_MIRROR" in
    0) CN_MIRROR_ON=0; ok "按参数使用官方源"; return 0 ;;
    1) CN_MIRROR_ON=1; read -r -a _m <<<"$PIP_MIRRORS"; PIP_INDEX_URL="${_m[0]}"
       PIP_TRUSTED_HOST="$(printf '%s' "$PIP_INDEX_URL" | sed -E 's#https?://([^/]+)/.*#\1#')"; return 0 ;;
  esac
  need_cmd curl || { CN_MIRROR_ON=0; return 0; }
  if [ -n "${PIP_INDEX_URL:-}" ] && probe_curl "$PIP_INDEX_URL"; then
    CN_MIRROR_ON=1; PIP_TRUSTED_HOST="${PIP_TRUSTED_HOST:-$(printf '%s' "$PIP_INDEX_URL" | sed -E 's#https?://([^/]+)/.*#\1#')}"
  elif pick_pip_mirror; then
    CN_MIRROR_ON=1
  else
    CN_MIRROR_ON=0
  fi
}

# 这个地址值不值得走加速：只有 github.com 在墙外
mirror_url() { # url
  if [ "$CN_MIRROR_ON" = "1" ] && [ -n "${GIT_MIRROR:-}" ]; then
    case "$1" in
      *://github.com/*|*://raw.githubusercontent.com/*) printf '%s%s' "$GIT_MIRROR" "$1"; return 0 ;;
    esac
  fi
  printf '%s' "$1"
}

is_cnb() {
  # 认三家：官方域名、镜像域名（API_HOST 含 slug）、以及「地址里就写着本仓库」的自建源。
  # 只认 cnb.cool 的话，换源之后 /-/git/archive 与 /-/git/raw 这套路径规则会被整段跳过。
  case "$GIT_HOST $API_HOST $REPO_URL" in
    *cnb.cool*) return 0 ;;
    *"$REPO_SLUG"*) return 0 ;;
  esac
  return 1
}

# 私有仓库：CNB_TOKEN（或环境里的 CNB 令牌）会被换成 Basic 头，可直接拉私库源码
# 带令牌请求时只发给 OpenAPI 域名。
#
# 给网页域名带上 Authorization 会被直接拒绝：cnb.cool 网关回 400
# （「Bad Request」，报文 42 字节），curl -f 于是判失败 —— 而这条 URL 恰好是
# 列表里的第一个。结果是：设了 CNB_TOKEN 的机器，取远端脚本和探版本一路全失败，
# 屏幕上只剩一句「探不到远端版本」，看起来像网络问题，其实是自己把请求写坏了。
# 所以网页域名用匿名请求，令牌只给 api.*。
auth_header() { # [host]
  local tok="${CNB_TOKEN:-}" host="${1:-}"
  [ -n "$tok" ] || return 0
  case "$host" in *"$API_HOST"*) : ;; *) return 0 ;; esac
  printf 'Authorization: Basic %s' "$(printf 'cnb:%s' "$tok" | base64 | tr -d '\n')"
}

fetch() { # url out
  local auth; auth="$(auth_header "$1")"
  if need_cmd curl; then
    if [ -n "$auth" ]; then curl -fL --retry 2 --connect-timeout 10 -m 300 -H "$auth" -o "$2" "$1"
    else curl -fL --retry 2 --connect-timeout 10 -m 300 -o "$2" "$1"; fi
  elif need_cmd wget; then
    if [ -n "$auth" ]; then wget -q -T 30 --header="$auth" -O "$2" "$1"
    else wget -q -T 30 -O "$2" "$1"; fi
  else die "需要 curl 或 wget"; fi
}

# 网络探测专用：短超时，失败返回非 0，绝不 die —— 探测失败只该降级，不该中止安装
probe_curl() { # url
  need_cmd curl || return 1
  curl -fsS -m 3 -o /dev/null "$1" 2>/dev/null
}

# ---------------------------------------------------------------- 版本
# 远端版本号：源码包里读得到。用来防止「旧脚本覆盖新代码」。
remote_version() { # dir
  local f="$1/app/__init__.py" v=""
  [ -f "$f" ] || { printf ''; return 0; }
  v="$(sed -n 's/^__version__[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$f" | head -1)"
  printf '%s' "$v"
}

script_version() { # 脚本文件读它自己声明的 VERSION
  [ -s "$1" ] || { printf ''; return 0; }
  sed -n 's/^VERSION="\([^"]*\)".*/\1/p' "$1" 2>/dev/null | head -1
}

version_lt() { # a b  —— a<b 时返回 0
  [ -n "$1" ] && [ -n "$2" ] || return 1
  [ "$1" = "$2" ] && return 1
  [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -1)" = "$1" ]
}

# 远端版本号。拉不到就输出空串（调用方自己决定当「看不出来」处理）。
# 只拉 app/__init__.py，比整包轻得多。
remote_peek_version() {
  need_cmd curl || need_cmd wget || return 0
  local url tmp v=""
  tmp="$(mktemp 2>/dev/null || echo "/tmp/.su-v.$$")"
  # 网页路由（/-/raw）在 CNB 上返回 200 的 HTML 外壳，从里面正则找版本永远找不到，
  # 于是「升级」看着跑了、实际什么都没换。有令牌才走 OpenAPI 的 git/raw。
  # 没有令牌也别直接跳过去猜：网页路由其实会 302 到带签名的真实文件地址，
  # `curl -L` 跟到底照样拿得到真源码，只是不能靠它判断「404 还是离线」——
  # 所以这里拿回来的是不是 Python 仍然由 is_python_file 把关。
  local urls=()
  if is_cnb; then
    # /-/raw/... 是前端路由，curl 回来是 200 的 HTML 外壳（「Page not found」那页），
    # 真正的裸文件在 /-/git/raw/...。这条排前面，/raw 留作别家平台的兜底。
    # API_HOST 与网页域名都试：换过镜像时只有一边是通的。
    urls+=("$GIT_HOST/$REPO_SLUG/-/git/raw/$REPO_BRANCH/app/__init__.py")
    [ -n "${CNB_TOKEN:-}" ] && urls+=("$API_HOST/$REPO_SLUG/-/git/raw/$REPO_BRANCH/app/__init__.py")
    [ "$API_HOST" != "$GIT_HOST" ] && urls+=("$API_HOST/$REPO_SLUG/-/git/raw/$REPO_BRANCH/app/__init__.py")
    urls+=("$GIT_HOST/$REPO_SLUG/-/raw/$REPO_BRANCH/app/__init__.py")
  else
    urls+=("$GIT_HOST/$REPO_SLUG/-/raw/$REPO_BRANCH/app/__init__.py")
  fi
  urls+=("https://raw.githubusercontent.com/${REPO_SLUG}/$REPO_BRANCH/app/__init__.py")
  for url in ${urls[@]+"${urls[@]}"}; do
    if fetch "$url" "$tmp" >/dev/null 2>&1 && is_python_file "$tmp"; then
      v="$(sed -n 's/^__version__[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$tmp" | head -1)"
      [ -n "$v" ] && break
    fi
  done
  rm -f "$tmp"
  printf '%s' "$v"
}

# 升级前问一句「远端和我一样吗」。看不出来（离线 / 没令牌 / 网络不通）时
# 返回 0 表示「不知道」，由调用方决定要不要继续——这里绝不能拿「不知道」冒充「一样」，
# 也不能拿它冒充「不一样」：前者让升级永远空转，后者让离线环境每次都白下载一遍。
REMOTE_PEEK=""      # 上一次探到的远端版本（空 = 没探到）
REMOTE_PEEKED="n"   # 是否探过（避免一次安装里重复拉取）
peek_remote() {
  [ "$REMOTE_PEEKED" = "y" ] && return 0
  REMOTE_PEEKED="y"
  REMOTE_PEEK="$(remote_peek_version)"
}

# 本地代码版本：优先读标记里的 code_version，旧标记退回直接看代码
local_code_version() {
  local v=""
  [ -s "$MARK_FILE" ] && v="$(sed -n 's/^code_version=//p' "$MARK_FILE" | head -1)"
  [ -n "$v" ] || v="$(remote_version "$INSTALL_DIR")"
  printf '%s' "$v"
}

# 0 = 远端与本地一致；1 = 不一致（或无本地版本）；2 = 看不出来
same_as_remote() {
  [ -s "$MARK_FILE" ] || return 1
  local local_v; local_v="$(local_code_version)"
  [ -n "$local_v" ] || return 1
  peek_remote
  [ -n "$REMOTE_PEEK" ] || return 2
  [ "$REMOTE_PEEK" = "$local_v" ] && return 0
  return 1
}

# ---------------------------------------------------------------- 环境体检
# 所有「装完才发现不对」的前置条件都放这里，一次说清楚，别让人猜。
DOCTOR_FAIL=0
DOCTOR_WARN=0
d_ok()   { printf "  %s✓%s %s\n" "$GREEN" "$RST" "$*"; }
d_warn() { printf "  %s!%s %s\n" "$YELLOW" "$RST" "$*"; DOCTOR_WARN=$((DOCTOR_WARN + 1)); }
d_bad()  { printf "  %s✗%s %s\n" "$RED" "$RST" "$*"; DOCTOR_FAIL=$((DOCTOR_FAIL + 1)); }

# 端口在不在监听。
#
# 早先只有 ss / lsof / netstat 三条路，三个都没有就 return 1（当成空闲）——
# minbase、slim 这类精简系统与多数 Docker 镜像就是这种状态，后果不是"探测不准"，
# 而是后面每一条"等端口放开"的循环都立刻放行：simpleurl restart 会在旧进程
# 还占着端口时就去启新的，新的 bind 失败，屏幕上却是一句启动成功。
# 现在多两条不需要任何外部命令的路：bash 的 /dev/tcp（能连上就是有人在听）
# 和内核的 /proc/net/tcp{,6}（十六进制地址:端口，任何 Linux 都有）。
port_in_use() { # port
  local p="$1" hex f
  # 四条路依次退，任何一条判出「有」就算数。以前是 elif 链：装了 ss 就只信 ss，
  # 而 ss 在非 root 下可能什么都看不见（别人的进程、受限容器），于是「端口被占」
  # 被判成「空闲」，紧接着启动撞上 EADDRINUSE，屏幕上却是一句启动成功。
  # 凡是「判有」的结论都不会误伤，多条路叠加只会更准。
  if need_cmd ss; then
    ss -lnt 2>/dev/null | awk -v p=":$p" '$4 ~ p"$" {found=1} END{exit !found}' && return 0
  fi
  if need_cmd lsof; then
    lsof -ti "tcp:$p" >/dev/null 2>&1 && return 0
  fi
  if need_cmd netstat; then
    netstat -lnt 2>/dev/null | awk -v p=":$p" '$4 ~ p"$" {found=1} END{exit !found}' && return 0
  fi
  # 内核视角：/proc/net/tcp 的端口是四位十六进制；:1F90 这种写法在 tcp4/tcp6 里都长一样。
  # 不需要任何外部命令，容器里也一定有。
  if [ -r /proc/net/tcp ]; then
    hex="$(printf '%04X' "$p" 2>/dev/null || echo "")"
    if [ -n "$hex" ]; then
      # 逐个文件读：/proc/net/tcp6 在没有 IPv6 的内核上不存在，合并重定向会把
      # 整条管道判成失败，反过来说成"端口空闲"——读不到当空闲，别当已占用。
      for f in /proc/net/tcp /proc/net/tcp6; do
        [ -r "$f" ] || continue
        awk -v h=":$hex" '$4 == "0A" && substr($2, length($2) - 4) == h {found=1} END{exit !found}' "$f" 2>/dev/null && return 0
      done
    fi
  fi
  # 最后一条：bash 自带的 /dev/tcp。连得上说明有人在听（只对 127.0.0.1 有意义，
  # 但本脚本起的服务本来就监听 0.0.0.0，够用）。用 subshell 收尾，别让
  # 连接错误把 set -e 的脚本带崩。
  (exec 3<>"/dev/tcp/127.0.0.1/$p") >/dev/null 2>&1 && return 0
  return 1
}

# 端口被谁占着、占着它的是不是「自己人」。命中返回 0 并把 pid 写进 PORT_HOLDER_PID。
# 用 ss 拿 pid 要 root；拿不到就没法判断，按「不是自己人」处理 —— 宁可不杀，
# 也不要因为认错而误杀别人的服务。
PORT_HOLDER_PID=""
# 谁在听这个端口？返回 pid 放在 PORT_HOLDER_PID。
#
# 以前只有 ss 一条路：而 ss 要装 iproute2，slim / minbase / 老 Debian 上默认没有，
# 于是「端口被占」的判定成立（port_in_use 有 /proc 兜底）、「占的是不是自己人」
# 却永远判不出来。两者一撞，重装时就报一句「端口被别的程序占用」——
# 占着的其实是刚被自己停掉、还在收尾的那一个。四种探测按可靠性依次退。
port_holder_pid() { # port
  PORT_HOLDER_PID=""
  local p="$1" hex inode pid line
  # 注意这里不能写成「有 ss 就直接 return 它的结果」：ss 在非 root 下、
  # 或者目标进程属于别的用户时，pid= 那一栏是空的。原来那种写法会带着一个
  # 空 pid 提前返回，后面 /proc 那条最可靠的兜底根本轮不到执行 —— 于是
  # 重装时把自己正在跑的服务判成「别的程序」，屏幕上多一句吓人的提醒。
  if need_cmd ss; then
    line="$(ss -lntp 2>/dev/null | awk -v p=":$p" '$4 ~ p"$" {print; exit}' || true)"
    PORT_HOLDER_PID="$(printf '%s' "$line" | sed -n 's/.*pid=\([0-9]*\).*/\1/p')"
    [ -n "$PORT_HOLDER_PID" ] && return 0
  fi
  # 内核视角：/proc/net/tcp 给出 socket 的 inode，再去 /proc/*/fd 里找谁拿着它。
  # 不需要任何外部命令，容器里也一定能用。
  if [ -r /proc/net/tcp ]; then
    hex="$(printf '%04X' "$p" 2>/dev/null || echo "")"
    if [ -n "$hex" ]; then
      inode="$(awk -v h=":$hex" '$4 == "0A" && substr($2, length($2) - 4) == h {print $10; exit}' \
        /proc/net/tcp /proc/net/tcp6 2>/dev/null | head -1)"
      if [ -n "$inode" ] && [ "$inode" != "0" ]; then
        pid="$(find /proc/[0-9]*/fd -lname "socket:\[$inode\]" 2>/dev/null \
          | sed -n 's|^/proc/\([0-9]*\)/fd/.*|\1|p' | head -1)"
        if [ -n "$pid" ]; then PORT_HOLDER_PID="$pid"; return 0; fi
      fi
    fi
  fi
  if need_cmd lsof; then
    pid="$(lsof -ti "tcp:$p" -sTCP:LISTEN 2>/dev/null | head -1)"
    [ -n "$pid" ] || pid="$(lsof -ti "tcp:$p" 2>/dev/null | head -1)"
    if [ -n "$pid" ]; then PORT_HOLDER_PID="$pid"; return 0; fi
  fi
  if need_cmd fuser; then
    pid="$(fuser -n tcp "$p" 2>/dev/null | tr -s ' ' '\n' | grep -E '^[0-9]+$' | head -1 || true)"
    if [ -n "$pid" ]; then PORT_HOLDER_PID="$pid"; return 0; fi
  fi
  return 1
}

# 这个 pid 是上次本脚本留下的服务进程吗？两条件：它自己的命令行落在安装目录里，
# 或者它就是标记文件里记下的那个 pid。除此之外一律不动。

doctor() {
  step "环境体检"
  # 有安装记录的目录：以标记里记的端口为准（doctor 常被单独调用，没有 --port）
  local port_override=""
  if [ -s "$MARK_FILE" ]; then
    port_override="$(sed -n 's/^port=//p' "$MARK_FILE" | head -1)"
  fi
  # 计数归零：装一次的流程里 doctor 可能被调两次（体检 + 失败后复查），
  # 不清零就会报出「4 项提醒」这种凭空的数字。
  DOCTOR_FAIL=0
  DOCTOR_WARN=0

  # 系统
  case "$(uname -s)" in
    Linux)  d_ok "系统 $(uname -s) $(uname -m)" ;;
    Darwin) d_ok "系统 macOS $(uname -m)" ;;
    *)      d_warn "未测过的系统 $(uname -s)，脚本可能跑不通" ;;
  esac
  if [ "$(uname -s)" = "Linux" ] && grep -qi microsoft /proc/version 2>/dev/null; then
    d_ok "运行在 WSL 里"
  fi

  # 运行方式
  if [ -n "$RUNTIME_OVERRIDE" ]; then
    case "$RUNTIME_OVERRIDE" in
      docker) have_docker && d_ok "Docker 可用（按参数指定）" || d_bad "指定了 --runtime docker，但 docker 不可用" ;;
      python) have_python && d_ok "Python 可用（按参数指定）" || d_bad "指定了 --runtime python，但找不到 python3" ;;
      *) d_bad "--runtime 只认 docker / python" ;;
    esac
  elif have_docker; then
    d_ok "Docker 可用 → 走容器部署"
  elif have_python; then
    d_warn "没有可用的 Docker → 走 Python 直装（更省资源，也够用）"
  else
    d_bad "既没有 Docker 也没有 Python 3.10+，需要先装一个"
  fi

  # Python 版本（即便走 Docker 也检查，换运行时不用重来）
  if need_cmd python3; then
    local pyv; pyv="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo "")"
    if [ -n "$pyv" ] && python3 -c 'import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)' 2>/dev/null; then
      d_ok "Python $pyv"
    else
      d_warn "Python ${pyv:-未知} 低于 3.10，Python 直装会失败"
    fi
    python3 -c 'import venv' 2>/dev/null || d_warn "Python 缺 venv 模块（apt install python3-venv）"
  fi

  # 用真实端口：只 `bash install.sh doctor`（不带 --port）时 PORT 还是默认值，
  # 报「8940 空闲」而实际服务跑在 18940，等于体检没做
  [ -n "${port_override:-}" ] && PORT="$port_override"
  # 有安装记录 = 这台机器上装过：端口是「装的时候那个」，冲突建议就不该是
  # 「自动换个口」——升级会照旧用记录里的端口启动。
  local has_install="n"
  [ -s "$MARK_FILE" ] && has_install="y"
  # 端口。三件事要分清，混在一起就会给出错的建议：
  #   · 空闲                        → 升级会用它，没问题
  #   · 有安装记录且在听             → 就是自己的服务，绝不会「自动改用别的端口」
  #   · 有安装记录但没人听           → 服务停了 / 挂了，这是提醒不是冲突
  #   · 没有安装记录却被占           → 真的冲突，装的时候会换口
  if ! port_in_use "$PORT"; then
    d_ok "端口 $PORT 空闲"
  elif port_owner_ours "$PORT"; then
    d_ok "端口 $PORT 由本目录的 SimpleURL 自己占着（重装会先停掉）"
  elif [ -n "${PORT_ORPHAN_PID:-}" ]; then
    d_ok "端口 $PORT 上是本目录留下的孤儿进程（安装时会清掉）"
  elif [ "$has_install" = "y" ]; then
    d_warn "端口 $PORT 被别的程序占用（升级仍按记录里的端口启动，届时可能起不来）"
  elif [ "$PORT_EXPLICIT" = "y" ]; then
    # 端口是用户指定的：装的时候不会「自动换口」，「会改用 …」那句话是假的
    d_warn "端口 $PORT 被别的程序占用（你指定了端口，不会自动换；占用它的进程还在就只有失败）"
  else
    local alt; alt="$(next_free_port "$PORT")"
    d_warn "端口 $PORT 被别的程序占用${alt:+，装的时候会自动改用 $alt}"
  fi

  # 时区：TZ 写错不会报错，只会让「今日点击」和过期时间整体偏掉几小时
  local tzfile="${TZ:-}"
  if [ -z "$tzfile" ]; then
    d_ok "时区 $(date +%Z)（时区正确的机器上，今日数据才和你的日历对得上）"
  elif [ -e "/usr/share/zoneinfo/$tzfile" ]; then
    d_ok "时区 $tzfile"
  else
    d_warn "TZ=$tzfile 在系统里找不到，容器里会退回 UTC，今日点击会偏"
  fi

  # 磁盘
  local avail_mb
  avail_mb="$(df -Pm "${HOME:-/tmp}" 2>/dev/null | awk 'NR==2{print $4}')"
  if [ -n "$avail_mb" ]; then
    if [ "$avail_mb" -ge 1024 ]; then d_ok "可用磁盘 ${avail_mb}MB"
    else d_warn "可用磁盘只有 ${avail_mb}MB，装依赖 + 镜像可能不够"; fi
  fi

  # 依赖
  need_cmd curl || need_cmd wget || d_bad "没有 curl / wget，下载源码会失败"
  need_cmd tar || d_bad "没有 tar"
  need_cmd git && d_ok "git 可用（升级更省流量）" || d_warn "没有 git，改用源码包升级（一样能升）"

  # 权限
  if [ "$(id -u)" = "0" ]; then
    d_ok "以 root 运行"
  elif need_cmd sudo && sudo -n true 2>/dev/null; then
    d_ok "可用免密 sudo（能注册 systemd 服务）"
  else
    d_warn "没有免密 sudo：跳过 systemd，用后台进程 + 开机自启兜底"
  fi

  # 安装目录里不该出现仓库的开发文件：requirements-dev.txt / pytest.ini / .cnb.yml
  # 只对「已经有安装」的目录查（首次安装时目录还是空的），而且只提醒不拦——
  # 它们不影响服务，但会让人以为安装目录等于仓库，照着自己去 pip install -r
  # requirements-dev.txt，装上一堆跟运行无关的东西。
  if [ "$has_install" = "y" ]; then
    local dev_left=""
    for _dev in requirements-dev.txt pytest.ini .cnb.yml; do
      [ -e "$INSTALL_DIR/$_dev" ] && dev_left="$dev_left $_dev"
    done
    [ -n "${dev_left// /}" ] && d_warn "安装目录里有开发文件${dev_left}（不影响运行，安装时会顺手清掉）"
  fi

  # 对外地址
  local ip; ip="$(detect_ip)"
  is_private_ip "$ip" && d_ok "本机地址 $ip" || d_ok "本机地址 $ip（公网）"

  printf "\n"
  if [ "$DOCTOR_FAIL" -gt 0 ]; then
    printf "%s%d 项阻塞，%d 项提醒%s\n" "$RED" "$DOCTOR_FAIL" "$DOCTOR_WARN" "$RST"
    return 1
  fi
  # --strict：把「提醒」也当阻塞。给 CI / 部署流水线用——它们需要「全绿才继续」，
  # 而默认的宽松口径（有提醒也能装）在流水线里等于什么都没拦。
  if [ "$STRICT" = "y" ] && [ "$DOCTOR_WARN" -gt 0 ]; then
    printf "%s%d 项提醒，按 --strict 判为不通过%s\n" "$RED" "$DOCTOR_WARN" "$RST"
    return 1
  fi
  [ "$DOCTOR_WARN" -gt 0 ] && printf "%s%d 项提醒，不挡安装%s\n" "$YELLOW" "$DOCTOR_WARN" "$RST"
  printf "%s可以装。%s\n" "$GREEN" "$RST"
  return 0
}

# ---------------------------------------------------------------- 源码
# 换代码前先把数据搬出来：数据是这个站唯一带不走的东西，代码随时能重下。
# 注意落点必须在本脚本自己算得出来的位置（.backup/<时间戳>/）：
# 回滚要按 PREV_BACKUP 去取，写到 $HOME 或用户指定路径就等于没备份。
backup_data() {
  local stamp; stamp="$(date +%Y%m%d-%H%M%S)"
  local dir="$INSTALL_DIR/.backup/$stamp"
  mkdir -p "$dir"
  [ -f "$INSTALL_DIR/simpleurl.db" ] && cp -a "$INSTALL_DIR/simpleurl.db"* "$dir/" 2>/dev/null || true
  [ -f "$INSTALL_DIR/.env" ] && cp -a "$INSTALL_DIR/.env" "$dir/" 2>/dev/null || true
  [ -f "$MARK_FILE" ] && cp -a "$MARK_FILE" "$dir/" 2>/dev/null || true
  # 只留最近 5 份：每次安装都备份一次，两年下来就是几十兆没用的小文件
  ls -1d "$INSTALL_DIR"/.backup/*/ 2>/dev/null | sort | head -n -5 | while read -r old; do
    [ -n "$old" ] && rm -rf "$old"
  done
  if [ -n "$(ls -A "$dir" 2>/dev/null)" ]; then
    # 只输出路径：这个函数的结果会被 $(...) 接走，中间夹一行「✓ …」就成了变量的一部分，
    # 屏幕上还会打出一句被截半的「上一版数据已备份到 ✓ …」
    printf '%s' "$dir"
    return 0
  fi
  rmdir "$dir" 2>/dev/null || true
  printf ''
  return 0
}

# 源码目录里认得的顶层条目：换代码时只管这些，其它（.venv/.env/数据/备份）一概不碰
# 注意不含 install.sh 和 simpleurl：它们自己就是入口/命令行，
# 覆盖成远端的旧版会让人下次跑到旧脚本
SOURCE_ENTRIES="app requirements.txt .env.example Dockerfile docker-compose.yml
.dockerignore .gitignore run.sh scripts README.md install.sh"
# 仓库里还有一批「开发用」文件：requirements-dev.txt、pytest.ini、.cnb.yml。
# 它们不是运行所需，以前却跟着一起装进安装目录 —— 于是安装目录看起来像仓库，
# 有人照着 README 在安装目录里 pip install -r requirements-dev.txt，装上一堆
# 跟服务无关的东西。现在不装了；老安装里残留的，换代码时顺手清掉。
DEV_ENTRIES="requirements-dev.txt pytest.ini .cnb.yml"
# 清单里的 install.sh 不能直接覆盖安装目录里那份：它此刻可能正在被执行
# （upgrade 就是从它跑起来的），覆盖到一半整份脚本就变样了，后面的函数全成
# 别人家的行号。所以先登记成「待更新」，等跑完再由 self_update_script 落盘。
SELF_UPDATE_PENDING="n"
SELF_SNAPSHOT=""
# `app/` 整体覆盖，但下面两类文件必须活下来：
#  · site.css / site.js —— 装完想改个配色、加段统计，最自然的做法就是写在这里。
#    升级把 app/ 整个换掉，等于每次升级都把用户的改动删一遍。
#  · logo/preview.html —— 出图工具的面板，只有跑了 scripts/gen_logo.py 才存在，
#    远端源码里没有，顺手删掉只是让人下次出图时发现少了东西
KEEP_ENTRIES="app/static/site.css app/static/site.js app/static/logo/preview.html"

# 早期版本把 app/ 里的模块平铺着拷进了安装根目录（main.py / core/ / templates/ …）。
# 换代码时这些副本必须一起清掉：它们既是伸手不见的垃圾，又会在 Python 的
# 包解析里和真正的 app/ 抢位置——`import core` 会先命中根目录那份。
LEGACY_ENTRIES="main.py web.py pages.py admin.py api.py services.py schemas.py models.py
qr.py core templates static tests admin_api.py yourls_api.py gen_logo.py __init__.py"

# 判断顺序很重要：先看「有没有平铺副本」，再清白名单。
# 反过来就永远清不掉——app/ 先被删掉之后，「根目录有 main.py」这个条件
# 仍然成立，可那时已经晚了，一级目录照样只剩 flat 那份。
clear_source() {
  local entry
  # 平铺副本只在「安装根目录里还躺着模块文件」时才清：正常安装根目录只有
  # app/ 一个包，出现 main.py / core/ 就说明是旧脚本留下的。判据从宽——
  # 这些名字在项目里本该只存在于 app/ 之下。
  if [ -f "$INSTALL_DIR/main.py" ] || [ -d "$INSTALL_DIR/core" ] || [ -d "$INSTALL_DIR/templates" ]; then
    warn "清理旧版残留的平铺源码（main.py / core/ 等会在 import 时和 app/ 抢位置）"
    for entry in $LEGACY_ENTRIES; do
      rm -rf "${INSTALL_DIR:?}/$entry"
    done
  fi
  for entry in $SOURCE_ENTRIES; do
    rm -rf "${INSTALL_DIR:?}/$entry"
  done
  # 开发文件不装，也不留：老安装里躺着的那几份在这里清掉。
  # 排在 SOURCE_ENTRIES 之后、落在「拷入新代码」之前，才不会把刚清干净的
  # 目录又脏回去。
  for entry in $DEV_ENTRIES; do
    rm -rf "${INSTALL_DIR:?}/$entry"
  done
}

# 下回来的「源码文件」是不是真的源码。CNB 的网页路由会返回 HTML 外壳，
# 只认「里面真有 __version__ 赋值」的才算拿到东西，别的都当没拿到。
is_python_file() { # file
  [ -s "$1" ] || return 1
  head -c 2 "$1" | grep -q '<' && return 1
  grep -q '^__version__' "$1" 2>/dev/null
}

is_tarball() { # file：先认 gzip 魔数，比 `file` 稳，也不依赖它
  [ -s "$1" ] || return 1
  [ "$(head -c 2 "$1" 2>/dev/null)" = "$(printf '\037\213')" ]
}

# 目标 URL 的 Content-Length，拿不到就打印空
remote_size() { # url
  need_cmd curl || return 0
  curl -fsSI -m 15 "$1" 2>/dev/null | awk 'BEGIN{IGNORECASE=1}/^content-length:/{gsub("\r","");print $2}' | tail -1
}

# 源码包解出来要不要削掉一层目录？看 tar 里第一个条目的路径：
#   `app/__init__.py` 这种以「仓库名/」开头的，削一层才落成 app/…
#   直接 `app/__init__.py` 开头的（CNB 的 /-/git/archive 就是这样），削一层会把
#   app/ 本身削掉，main.py / core/ 平铺到安装根目录 —— 文件一个不少、服务也起得来，
#   于是没人发现装出来的是一份和仓库不一样的副本，升级还会再砸一遍。
tar_strip_level() { # tarball —— 0 或 1
  local first
  first="$(tar -tzf "$1" 2>/dev/null | head -1)" || return 0
  # 只有一个顶层名字、且路径里确实带斜杠：才值得削
  case "$first" in
    */?*) printf '1' ;;
    *) printf '0' ;;
  esac
}

extract_tarball() { # tarball stage
  local tarball="$1" stage="$2" strip
  strip="$(tar_strip_level "$tarball")"
  [ -n "$strip" ] || strip=0
  tar -xzf "$tarball" -C "$stage" --strip-components="$strip" 2>/dev/null || return 1
  # 解完还是有嵌套的 app/app/：说明第一层猜错了，翻过来再试一次
  if [ ! -f "$stage/app/main.py" ] && [ -f "$stage/app/app/main.py" ]; then
    tar -xzf "$tarball" -C "$stage" --strip-components=1 2>/dev/null || return 1
  fi
  [ -f "$stage/app/main.py" ]
}

stage_source() { # 把最新代码放到 $1（一个空目录）。失败返回非 0
  local stage="$1" urls=() u tarball
  tarball="$stage/.src.tar.gz"

  # 目录里的脚本给自己跑（安装目录里的 install.sh），不用重复下载；
  # 但 /workspace 拷出去的脚本没有 install.sh，得从远端取 —— 否则装出来是半截。
  # `upgrade` 例外：升级的全部意义就是「拿远端那份」，拿本地现成的等于原地转圈，
  # 还会报一句「源码已就绪」骗人。
  #
  # 判据带上版本：脚本可能比目录里的代码新（升级流程就是一例 —— 换壳用的是远端
  # 脚本，但换壳后自己判断「脚本在安装目录里」就直接沿用旧代码，等于没升）。
  # 脚本版本更新时必须照常走下载，否则「升了脚本、没升代码」会一直这么歪着。
  if [ "$ACTION_UPGRADE" != "y" ] && [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/app/main.py" ] \
     && [ "$SELF_DIR" = "$(cd "$INSTALL_DIR" 2>/dev/null && pwd || echo "")" ] \
     && [ -f "$SELF_DIR/install.sh" ]; then
    if ! [ -f "$MARK_FILE" ] || ! version_lt "$(sed -n 's/^version=//p' "$MARK_FILE" | head -1)" "$VERSION"; then
      return 2
    fi
    warn "脚本比安装目录里的旧（v$(sed -n 's/^version=//p' "$MARK_FILE" | head -1) → v$VERSION），照常取源码"
  fi
  # 从仓库目录里直接装（README 的第一种用法 ./install.sh --yes）也一样：
  # 脚本身边就是完整源码，没有理由再下一遍。
  # 但只有「搬到别处」时才省这一步。目标目录就是仓库自己时不能再省——
  # 那就成了原地踏步：什么都拦下了，可依赖、venv、启动一样都没做。
  if [ "$ACTION_UPGRADE" != "y" ] && [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/app/main.py" ] \
     && [ -f "$SELF_DIR/install.sh" ] && [ -f "$SELF_DIR/.cnb.yml" ] \
     && [ "$SELF_DIR" != "$(cd "$INSTALL_DIR" 2>/dev/null && pwd || echo "")" ]; then
    return 2
  fi

  # 源码包优先：不依赖 git，也不受仓库体积影响
  [ "$(mirror_url "$REPO_TARBALL")" != "$REPO_TARBALL" ] && urls+=("$(mirror_url "$REPO_TARBALL")")
  urls+=("$REPO_TARBALL")
  # 换过镜像 / 自建源时，GIT_HOST 与 API_HOST 只有一个是通的。补上另一半。
  if is_cnb; then
    case "$REPO_TARBALL" in
      *"$API_HOST"*) urls+=("${REPO_TARBALL/"$API_HOST"/"$GIT_HOST"}") ;;
      *"$GIT_HOST"*) urls+=("${REPO_TARBALL/"$GIT_HOST"/"$API_HOST"}") ;;
    esac
  fi
  for u in ${urls[@]+"${urls[@]}"}; do
    rm -f "$tarball"
    # is_tarball 认 gzip 魔数，网页外壳（HTML）在这一步就被挡掉
    if fetch "$u" "$tarball" >/dev/null 2>&1 && is_tarball "$tarball"; then
      if extract_tarball "$tarball" "$stage"; then
        rm -f "$tarball"
        return 0
      fi
      warn "源码包解压失败，换下一个地址"
    fi
    rm -f "$tarball"
  done

  # 走到这里说明源码包没拿到。CNB 的网页归档地址在无令牌时必定给出 HTML 外壳，
  # 所以「升级」会悄悄退化成 git clone；两者都不通最后才报错。
  if need_cmd git; then
    local clone_url; clone_url="$(mirror_url "$REPO_URL")"
    if git clone --depth 1 --branch "$REPO_BRANCH" "$clone_url" "$stage/clone" >/dev/null 2>&1; then
      cp -a "$stage/clone/." "$stage/"
      rm -rf "$stage/clone" "$stage/.git"
      [ -f "$stage/app/main.py" ] && return 0
    fi
    rm -rf "$stage/clone"
  fi
  return 1
}

# 用户放在 app/ 里的自定义文件：换代码前挪走，换完放回来。
# 不这么做的话，升级会把 site.css / site.js 一起删掉 —— 那是用户唯一
# 能改样式又不用碰源码的地方，每次升级清一遍等于逼人 fork。
stash_user_files() { # 返回暂存目录（没有要留的就返回空）
  local dir="$1" f keep=""
  for f in $KEEP_ENTRIES; do
    # preview.html 是出图工具的产物，不是用户改动，不用提示
    [ -f "$INSTALL_DIR/$f" ] && keep="$keep $f"
  done
  [ -n "$keep" ] || { printf ''; return 0; }
  for f in $keep; do
    mkdir -p "$dir/$(dirname "$f")"
    cp -a "$INSTALL_DIR/$f" "$dir/$f" 2>/dev/null || true
  done
  printf '%s' "$dir"
}

# 把 $1 里的源码替换进安装目录（先清白名单条目，再整体拷过去）
# 安装目录里的 install.sh 必须活过每一次落盘。
#
# clear_source 会按 SOURCE_ENTRIES 把 install.sh 删掉（它在清单里，因为「脚本也要
# 能自更新」）。删掉之后能不能补回来，原来只挂在「源码包里带了 install.sh」这一条上：
# 带了才在 clear_source 之前留影子，不带就什么都不留 —— 而 CNB 的源码包正好不带。
# 于是从仓库目录装出来的机器，第一次升级就把自己的 install.sh 弄丢，
# `simpleurl upgrade / doctor / uninstall` 从此一律报「缺少 install.sh」。
# 现在无条件留影子：有就留，没有就把正在跑的这份（$SELF_PATH）留一份。
preserve_self() { # stage
  local stage="$1" src=""
  if [ -f "$INSTALL_DIR/install.sh" ]; then
    src="$INSTALL_DIR/install.sh"
  elif [ -n "$SELF_DIR" ] && [ -f "$SELF_PATH" ]; then
    src="$SELF_PATH"
  fi
  [ -n "$src" ] || return 0
  cp -f "$src" "$stage/.install.sh.before" 2>/dev/null || return 0
  SELF_SNAPSHOT="$stage/.install.sh.before"
}

apply_source() { # stage
  local stage="$1" stash="" f
  stash="$(stash_user_files "$stage/.userfiles")"
  # 这一步必须排在 clear_source 之前 —— 那个函数会先把 SOURCE_ENTRIES 里的
  # install.sh 删掉，删完再来判断「有没有旧脚本」，永远判成没有，自更新就
  # 静默失效：安装目录里永远是装那天的脚本，新脚本里的修法一个都用不上。
  preserve_self "$stage"
  # stage 里的 install.sh 只作「待更新」证据，随后立刻摘掉：clear_source 会按
  # SOURCE_ENTRIES 删安装目录里的 install.sh，紧接着 `cp -a "$stage/."` 把它拷回来。
  # 这一对必须成对出现，少一半就是两种事故：先拷后删 = 把自己正在跑的脚本删掉
  # （upgrade 中途整份脚本消失）；先删后不拷 = 安装目录里再没有 install.sh，
  # 之后的 upgrade / doctor / uninstall 一律报「缺少 install.sh」。
  if [ -f "$stage/install.sh" ]; then
    if [ -f "$INSTALL_DIR/install.sh" ] && cmp -s "$stage/install.sh" "$INSTALL_DIR/install.sh"; then
      # 一模一样，不用更新（也就不用留 pending）
      rm -f "$stage/install.sh"
    else
      cp -f "$stage/install.sh" "$INSTALL_DIR/.install.sh.pending"
      SELF_UPDATE_PENDING="y"
      rm -f "$stage/install.sh"
    fi
  fi
  clear_source
  # 解压出来的 `.git` 之类不要带进来
  rm -rf "$stage/.git"
  cp -a "$stage/." "$INSTALL_DIR/"
  if [ -n "$stash" ]; then
    local kept=""
    for f in $KEEP_ENTRIES; do
      [ -f "$stash/$f" ] || continue
      mkdir -p "$INSTALL_DIR/$(dirname "$f")"
      cp -a "$stash/$f" "$INSTALL_DIR/$f"
      kept="$kept ${f##*/}"
    done
    [ -n "${kept// /}" ] && ok "你自己放的静态文件已保留：$kept"
  fi
  rm -rf "$stage/.userfiles"
  # 安装目录里没有 install.sh 就补回去：源码没带、更新没成、或上一轮被杀在半路。
  # 少了它，simpleurl upgrade / doctor / uninstall 全部报「缺少 install.sh」。
  if [ ! -f "$INSTALL_DIR/install.sh" ] && [ -s "${SELF_SNAPSHOT:-}" ]; then
    cp -f "$SELF_SNAPSHOT" "$INSTALL_DIR/install.sh" 2>/dev/null \
      && chmod +x "$INSTALL_DIR/install.sh" 2>/dev/null || true
  fi
  rm -f "${SELF_SNAPSHOT:-}" "$stage/.install.sh.before"
  SELF_SNAPSHOT=""
}

# 脚本身边那份源码：拷到 stage，形状和远端包解出来的一样（带 app/）
copy_local_source() { # stage
  local stage="$1" e
  [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/app/main.py" ] || return 1
  mkdir -p "$stage"
  for e in $SOURCE_ENTRIES; do
    [ -e "$SELF_DIR/$e" ] || continue
    cp -a "$SELF_DIR/$e" "$stage/" 2>/dev/null || true
  done
  [ -f "$stage/app/main.py" ]
}

install_source() {
  mkdir -p "$INSTALL_DIR"
  local stage="$INSTALL_DIR/.stage"
  rm -rf "$stage"; mkdir -p "$stage"

  step "获取源码"
  local rc=0
  stage_source "$stage" || rc=$?
  if [ "$rc" = "2" ]; then
    rm -rf "$stage"
    # 只有安装目录里真的有源码，才谈得上「一致」。
    if [ -f "$INSTALL_DIR/app/main.py" ]; then
      ok "本地版本与安装目录一致，跳过下载"
      SOURCE_REUSED="y"
      # 说「一致」只对代码成立：上一版脚本留下的开发文件（requirements-dev.txt
      # 等）还躺在安装目录里，它们是「不装」的那一类，顺手清掉，别等下次换代码。
      local dev; local dev_cleaned=""
      for dev in $DEV_ENTRIES; do
        if [ -e "$INSTALL_DIR/$dev" ]; then rm -rf "${INSTALL_DIR:?}/$dev"; dev_cleaned="$dev_cleaned $dev"; fi
      done
      [ -n "${dev_cleaned// /}" ] && ok "清掉旧版留下的开发文件：$dev_cleaned"
      return 0
    fi
    # 脚本身边就是完整源码，直接拷过去 —— 比再下一次包稳，也快得多
    warn "脚本身边有源码，但 $INSTALL_DIR 里还没有，直接用这一份"
    mkdir -p "$stage"
    copy_local_source "$stage" || die "从 $SELF_DIR 复制源码失败"
    rc=0
  fi
  if [ "$rc" != "0" ]; then
    rm -rf "$stage"
    # 已有安装 / 离线环境：「下载失败」不等于不能继续
    if [ -f "$INSTALL_DIR/app/main.py" ]; then
      warn "拿不到远端源码（地址：$REPO_TARBALL / git clone 也不通）"
      warn "本轮沿用 $INSTALL_DIR 里已有的代码，**代码没有更新**"
      warn "  · 想指定来源：REPO_TARBALL=<源码包地址> 或 REPO_URL=<git 地址>"
      warn "  · 网络受限时先装好代理 / 配 GIT_HOST，再 simpleurl upgrade"
      SOURCE_REUSED="y"
      return 0
    fi
    die "源码下载失败。可手动下载后解压到 $INSTALL_DIR 再重跑本脚本"
  fi

  # 防降级：远端比本地旧时不再覆盖，否则「升级」会把人升回旧版。
  # 只比代码版本（app/__init__.py），不要比脚本版本 —— 早期版本把脚本自己的
  # VERSION 写进了标记文件，结果 2.0.0 的脚本对上 1.5.0 的代码就判定「远端更旧」，
  # 从此再也升不动。标记文件里 code_version 才是代码版本。
  local new_v old_v
  new_v="$(remote_version "$stage")"
  old_v=""
  if [ -f "$MARK_FILE" ]; then
    old_v="$(sed -n 's/^code_version=//p' "$MARK_FILE" | head -1)"
    if [ -z "$old_v" ]; then
      # 旧标记：没有 code_version 字段。本地就有代码的话以代码为准，否则认版本字段
      if [ -f "$INSTALL_DIR/app/__init__.py" ]; then
        old_v="$(remote_version "$INSTALL_DIR")"
      else
        old_v="$(sed -n 's/^version=//p' "$MARK_FILE" | head -1)"
      fi
    fi
  fi
  if version_lt "$new_v" "$old_v"; then
    warn "远端版本 $new_v 比本地 $old_v 旧，保留本地代码（换 REPO_BRANCH=<分支> 可强行覆盖）"
    rm -rf "$stage"
    SOURCE_REUSED="y"
    return 0
  fi

  if [ -f "$INSTALL_DIR/app/main.py" ]; then
    # 备份路径要留下来给 rollback_or_die —— 先备份后回滚，
    # 只把路径打到屏幕上，等于出事时没人知道该拿哪一份回去
    PREV_BACKUP="$(backup_data)"
    if [ -n "$PREV_BACKUP" ]; then ok "旧数据已备份：$PREV_BACKUP"
    else warn "这一轮没备份出东西（首次安装时正常）"; fi
  fi

  # 解到临时目录再替换：解压半路失败时，现有代码还是完整的
  apply_source "$stage"
  rm -rf "$stage"
  ok "源码已就绪${new_v:+（v$new_v）}"
}

# ---------------------------------------------------------------- Docker
have_docker() { need_cmd docker && docker info >/dev/null 2>&1; }
have_python() {
  need_cmd python3 && python3 -c 'import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)' 2>/dev/null
}

configure_docker_mirror() {
  [ "$CN_MIRROR_ON" = "1" ] || return 0
  local path="/etc/docker/daemon.json"
  [ "$(uname -s)" = "Darwin" ] && path="$HOME/.docker/daemon.json"
  if [ -f "$path" ] && grep -q registry-mirrors "$path" 2>/dev/null; then
    ok "已有 Docker 加速配置，保持不动"; return 0
  fi
  local SUDO=""
  [ "$(id -u)" != "0" ] && need_cmd sudo && SUDO="sudo"
  if [ "$(id -u)" != "0" ] && [ -z "$SUDO" ]; then
    warn "没有 root 权限，跳过 Docker 加速配置"; return 0
  fi
  step "配置 Docker 镜像加速"
  local json; json="$(printf '%s' "$DOCKER_MIRRORS" | tr ',' '\n' | sed 's/.*/"&"/' | paste -sd, -)"
  if [ -f "$path" ]; then
    if need_cmd jq; then
      $SUDO cp "$path" "$path.bak.$(date +%s)"
      $SUDO sh -c "jq '. + {\"registry-mirrors\": [$json]}' '$path' > /tmp/daemon.$$.json && mv /tmp/daemon.$$.json '$path'"
    else
      warn "$path 已存在且没有 jq，跳过自动合并"; return 0
    fi
  else
    mkdir -p "$(dirname "$path")" 2>/dev/null || true
    printf '{\n  "registry-mirrors": [%s]\n}\n' "$json" | $SUDO tee "$path" >/dev/null
  fi
  need_cmd systemctl && $SUDO systemctl restart docker >/dev/null 2>&1 || true
  ok "Docker 加速已写入 $path"
}

docker_pull() { # image
  local image="$1" m1 m2
  m1="$(printf '%s' "$DOCKER_MIRRORS" | cut -d, -f1)"
  m2="$(printf '%s' "$DOCKER_MIRRORS" | cut -d, -f2)"
  step "拉取基础镜像 $image（走加速源，通常几十秒）"
  local c
  for c in "$image" "$m1/$image" "$m2/$image"; do
    if timeout 420 docker pull "$c" >/dev/null 2>&1; then
      [ "$c" != "$image" ] && docker tag "$c" "$image" 2>/dev/null || true
      return 0
    fi
  done
  return 1
}

# 生成一份等价 compose 文件：以后不用记一长串 docker run，
# `cd 安装目录 && docker compose up -d` 就能重建同一个服务。
write_compose_file() {
  local f="$INSTALL_DIR/$COMPOSE_FILE_NAME"
  cat > "$f" <<EOF
# 由 install.sh $VERSION 生成于 $(date '+%Y-%m-%d %H:%M:%S')
# 改端口 / 环境变量后：docker compose up -d --force-recreate
# 不想用 compose 也行，simpleurl start|stop|restart 直接管容器
services:
  $SERVICE_NAME:
    image: simpleurl:latest
    container_name: $SERVICE_NAME
    restart: unless-stopped
    ports:
      - "$PORT:8000"
    env_file:
      - .env.docker
    volumes:
      - ${SERVICE_NAME}-data:/data
    labels:
      simpleurl.managed: "1"

volumes:
  ${SERVICE_NAME}-data:
    external: true
EOF
  ok "compose 文件：$f"
}

# 容器里的配置以 /data/.env 为准，宿主机上的这是一份可编辑的镜像。
# 注意不能用 `docker exec ... printf` 写：容器里没有 printf 内置、
# 宿主 shell 又会把引号吃掉，最后写进去的是空文件。
write_docker_env() {
  local envfile="$INSTALL_DIR/.env.docker"
  {
    printf '# 容器内 /data/.env 的副本（由 install.sh %s 生成）\n' "$VERSION"
    printf 'SITE_NAME=%s\n' "$SITE_NAME"
    printf 'PORT=8000\n'
    printf 'PUBLIC_URL=%s\n' "$PUBLIC_URL"
    printf 'ACCESS_MODE=%s\n' "$ACCESS_MODE"
    printf 'DATABASE_URL=%s\n' "sqlite:////data/simpleurl.db"
    printf 'SECRET_KEY=%s\n' "$SECRET_KEY"
    printf 'ADMIN_USERNAME=%s\n' "$ADMIN_USER"
    printf 'ADMIN_PASSWORD=%s\n' "$ADMIN_PASS"
    printf 'TZ=%s\n' "${TZ:-Asia/Shanghai}"
  } > "$envfile"
  chmod 600 "$envfile" 2>/dev/null || true
}

install_docker_mode() {
  step "Docker 方式部署"
  configure_docker_mirror
  write_docker_env

  local build_args=()
  if [ "$CN_MIRROR_ON" = "1" ] && [ -n "$PIP_INDEX_URL" ]; then
    build_args=(--build-arg "PIP_INDEX_URL=$PIP_INDEX_URL" --build-arg "PIP_TRUSTED_HOST=$PIP_TRUSTED_HOST")
  fi
  docker_pull "python:3.12-slim" || warn "基础镜像拉取受阻，继续尝试构建"
  step "构建镜像（首次会装依赖）"
  if ! docker build ${build_args[@]+"${build_args[@]}"} -t simpleurl:latest \
        -f "$INSTALL_DIR/Dockerfile" "$INSTALL_DIR" >"$INSTALL_DIR/.build.log" 2>&1; then
    tail -n 15 "$INSTALL_DIR/.build.log" 2>/dev/null | sed 's/^/    /' >&2
    rollback_or_die "镜像构建失败（日志：$INSTALL_DIR/.build.log）"
  fi
  ok "镜像构建完成"

  # 已有容器先记下镜像 ID，起不来能用它回去
  local prev_image=""
  prev_image="$(docker inspect -f '{{.Image}}' "$SERVICE_NAME" 2>/dev/null || echo "")"
  write_compose_file

  docker rm -f "$SERVICE_NAME" >/dev/null 2>&1 || true
  docker volume create "${SERVICE_NAME}-data" >/dev/null
  docker run -d --name "$SERVICE_NAME" --restart unless-stopped \
    -p "${PORT}:8000" -v "${SERVICE_NAME}-data:/data" \
    -e "SITE_NAME=$SITE_NAME" -e "PORT=8000" \
    -e "DATABASE_URL=sqlite:////data/simpleurl.db" \
    -e "SECRET_KEY=$SECRET_KEY" \
    -e "ADMIN_USERNAME=$ADMIN_USER" -e "ADMIN_PASSWORD=$ADMIN_PASS" \
    -e "PUBLIC_URL=$PUBLIC_URL" -e "ACCESS_MODE=$ACCESS_MODE" \
    -e "TZ=${TZ:-Asia/Shanghai}" \
    -e "SIMPLEURL_ENV_FILE=/data/.env" \
    --label "simpleurl.managed=1" \
    --label "simpleurl.dir=$INSTALL_DIR" \
    simpleurl:latest >/dev/null || rollback_or_die "容器启动失败"
  RUNTIME="docker"
  ok "容器已启动：$SERVICE_NAME（数据卷 ${SERVICE_NAME}-data）"
  # 这一步以前只在 Python 路上做。Docker 装完不验，等于「容器起来了」就算成功：
  # 凭据写错、端口映射错、镜像里是旧代码，全都要等运维自己发现。
  if [ "$SKIP_HEALTH" != "y" ]; then
    wait_health 60 || rollback_or_die "容器已启动但健康检查没过（日志：docker logs $SERVICE_NAME）"
    verify_end_to_end || rollback_or_die "容器能跑，但业务自检没过（登录 / 建链 / 跳转任一失败）"
  fi

  # /data/.env 是「容器里的那份」，让 simpleurl config / password 改得动
  if ! docker exec "$SERVICE_NAME" sh -c 'cat > /data/.env && chmod 600 /data/.env' \
        < "$INSTALL_DIR/.env.docker" >/dev/null 2>&1; then
    warn "写容器配置文件失败，改配置请用 docker exec 手动处理"
  fi
  docker restart "$SERVICE_NAME" >/dev/null 2>&1 || true
  PREV_IMAGE="$prev_image"
}

# ---------------------------------------------------------------- Python
pkg_install() {
  local SUDO=""
  [ "$(id -u)" != "0" ] && need_cmd sudo && SUDO="sudo"
  if need_cmd apt-get; then $SUDO apt-get update -qq && $SUDO apt-get install -y -qq "$@"
  elif need_cmd dnf; then $SUDO dnf install -y "$@"
  elif need_cmd yum; then $SUDO yum install -y "$@"
  elif need_cmd apk; then $SUDO apk add --no-cache "$@"
  else return 1; fi
}

ensure_python() {
  if ! need_cmd python3; then
    step "安装 python3"
    pkg_install python3 python3-venv python3-pip || die "请手动安装 Python 3.10+"
  fi
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' \
    || die "需要 Python 3.10+，当前 $(python3 -V 2>&1)"
}

pip_install() {
  local pip="$1"; shift
  local opts=(-q --disable-pip-version-check --root-user-action=ignore)
  if [ "$CN_MIRROR_ON" = "1" ] && [ -n "$PIP_INDEX_URL" ]; then
    if "$pip" install "${opts[@]}" -i "$PIP_INDEX_URL" --trusted-host "${PIP_TRUSTED_HOST:-}" "$@"; then
      return 0
    fi
    warn "镜像源不可用，回落官方源重试"
  fi
  "$pip" install "${opts[@]}" "$@"
}

# requirements.txt 的指纹。升级时如果依赖清单变了（新增包、改版本），
# 只重启进程是不够的——import 会当场炸。指纹一致才跳过安装。
deps_fingerprint() { # file
  [ -f "$1" ] || { printf ''; return 0; }
  if need_cmd sha256sum; then sha256sum "$1" | cut -d' ' -f1
  elif need_cmd shasum; then shasum -a 256 "$1" | cut -d' ' -f1
  else cksum "$1" | cut -d' ' -f1-2 | tr -d ' '; fi
}
DEPS_FILE="$INSTALL_DIR/.deps.sha256"

install_python_mode() {
  step "Python 方式部署（venv）"
  ensure_python
  local want_deps
  want_deps="$(deps_fingerprint "$INSTALL_DIR/requirements.txt")"
  if [ -x "$INSTALL_DIR/.venv/bin/python" ] && [ -f "$DEPS_FILE" ] \
     && [ -n "$want_deps" ] && [ "$(cat "$DEPS_FILE" 2>/dev/null)" = "$want_deps" ] \
     && "$INSTALL_DIR/.venv/bin/python" -c "import fastapi, uvicorn, httpx" 2>/dev/null; then
    # 依赖没变就别重装：升级二十秒能完事，不必每次等 pip 转圈
    ok "依赖清单未变化，跳过安装"
    RUNTIME="python"
    return 0
  fi
  if ! python3 -m venv "$INSTALL_DIR/.venv" 2>/dev/null; then
    warn "创建 venv 失败，补装 python3-venv"
    pkg_install python3-venv python3-pip || true
    python3 -m venv "$INSTALL_DIR/.venv" || rollback_or_die "创建虚拟环境失败，请手动安装 python3-venv"
  fi
  local pip="$INSTALL_DIR/.venv/bin/pip"
  step "安装依赖（通常 1~2 分钟）"
  pip_install "$pip" --upgrade pip >/dev/null 2>&1 || true
  pip_install "$pip" -r "$INSTALL_DIR/requirements.txt" || rollback_or_die "依赖安装失败，检查网络后重试"
  # 装完自检：缺包在这里说清楚，比等 uvicorn 抛一屏 import 栈好读
  if ! "$pip" check >/dev/null 2>&1; then
    warn "依赖有冲突，仍继续启动（可用 simpleurl log 查详情）"
  fi
  if ! "$INSTALL_DIR/.venv/bin/python" -c "import fastapi, uvicorn, httpx" 2>/dev/null; then
    warn "依赖没装全，正在用官方源补装一次"
    "$pip" install -q --disable-pip-version-check -r "$INSTALL_DIR/requirements.txt" \
      || rollback_or_die "依赖装不上。可手改 $INSTALL_DIR/requirements.txt 后重跑"
  fi
  [ -n "$want_deps" ] && printf '%s\n' "$want_deps" > "$DEPS_FILE"
  RUNTIME="python"
}

# 装进去的代码要能被它自己的解释器 import，版本还要和安装目录里的源码一致。
# 两件事都只信「跑起来的结果」，不信标记文件。
check_installed_code() {
  local got="" want
  want="$(remote_version "$INSTALL_DIR")"
  [ -n "$want" ] || return 0        # 没有 __version__ 就没什么可比的，交给健康检查
  case "$RUNTIME" in
    docker)
      # 容器里问的是 /data 外的同一份代码：镜像构建时 COPY 进去的 app/
      got="$(docker exec "$SERVICE_NAME" python -c \
        'import app;print(app.__version__)' 2>/dev/null || true)" ;;
    *)
      [ -x "$INSTALL_DIR/.venv/bin/python" ] || return 0
      got="$(cd "$INSTALL_DIR" && "$INSTALL_DIR/.venv/bin/python" -c \
        'import app;print(app.__version__)' 2>/dev/null || true)" ;;
  esac
  if [ -z "$got" ]; then
    warn "装进去的 app 连 import 都过不了，多半是依赖没装全"
    return 1
  fi
  if [ "$got" != "$want" ]; then
    warn "装进目录的代码是 v$got，目录里的源码写着 v$want —— 目录被别的东西覆盖过"
    return 1
  fi
  ok "代码自检通过（app v$got 可 import）"
  return 0
}

write_env() {
  local file="$1"
  umask 077
  cat > "$file" <<EOF
# SimpleURL 配置，由 install.sh $VERSION 生成于 $(date '+%Y-%m-%d %H:%M:%S')
# 改完执行 simpleurl restart 生效；也可以 simpleurl config 直接编辑
SITE_NAME=$SITE_NAME
HOST=0.0.0.0
PORT=$PORT
PUBLIC_URL=$PUBLIC_URL
ACCESS_MODE=$ACCESS_MODE
DATABASE_URL=$DATABASE_URL
SECRET_KEY=$SECRET_KEY
TZ=${TZ:-Asia/Shanghai}
# 后台凭据：登录页用账号密码；ADMIN_TOKEN 是脚本 / CI 的兼容入口（X-Admin-Token 头）
ADMIN_USERNAME=$ADMIN_USER
ADMIN_PASSWORD=$ADMIN_PASS
EOF
  chmod 600 "$file" 2>/dev/null || true
}

# 启动包装。用 exec 收尾，pid 文件里记的就是真正的 uvicorn 进程，
# 脚本退出后 kill 得掉，不会留孤儿。
write_start_wrapper() {
  cat > "$INSTALL_DIR/start.sh" <<EOF
#!/usr/bin/env sh
# 由 install.sh 生成：载入 .env 后前台启动（交给 systemd / simpleurl 管理）
set -e
cd "$INSTALL_DIR"
set -a
. ./.env
set +a
exec ./.venv/bin/python -m uvicorn app.main:app --host "\${HOST:-0.0.0.0}" --port "\${PORT:-8000}"
EOF
  chmod +x "$INSTALL_DIR/start.sh"
}

# 有 setsid 就脱离终端（关掉 SSH 也不死），没有就普通 nohup
spawn_detached() { # logfile
  local log="$1"
  if need_cmd setsid; then
    setsid nohup "$INSTALL_DIR/start.sh" >>"$log" 2>&1 </dev/null &
  else
    nohup "$INSTALL_DIR/start.sh" >>"$log" 2>&1 </dev/null &
  fi
  printf '%s' "$!"
}

start_background() {
  step "后台启动服务"
  local pid; pid="$(spawn_detached "$LOG_FILE")"
  echo "$pid" > "$INSTALL_DIR/simpleurl.pid"
  sleep 2
  if kill -0 "$pid" 2>/dev/null; then
    ok "已启动，PID $pid"
    return 0
  fi
  warn "进程没起来，最近日志："; tail -n 12 "$LOG_FILE" 2>/dev/null | sed 's/^/    /'
  return 1
}

has_systemd_unit() { [ -f "/etc/systemd/system/${SERVICE_NAME}.service" ]; }

setup_systemd() {
  [ "$RUNTIME" = "python" ] || return 1
  need_cmd systemctl || return 1
  [ -d /run/systemd/system ] || return 1
  local SUDO=""
  [ "$(id -u)" != "0" ] && need_cmd sudo && SUDO="sudo"
  if [ "$(id -u)" != "0" ] && [ -z "$SUDO" ]; then
    warn "没有 root 权限，跳过 systemd（用 simpleurl start 手动管理）"; return 1
  fi
  $SUDO tee "/etc/systemd/system/${SERVICE_NAME}.service" >/dev/null <<EOF
[Unit]
Description=SimpleURL short link service
After=network.target

[Service]
Type=simple
ExecStart=$INSTALL_DIR/start.sh
Restart=always
RestartSec=3
WorkingDirectory=$INSTALL_DIR

[Install]
WantedBy=multi-user.target
EOF
  $SUDO systemctl daemon-reload >/dev/null 2>&1 || true
  if $SUDO systemctl enable --now "$SERVICE_NAME" >/dev/null 2>&1; then
    ok "已注册为系统服务：systemctl status $SERVICE_NAME"; return 0
  fi
  warn "systemd 注册失败，改用后台进程"; return 1
}

# 这个容器是不是本目录的？认 labels；老容器没打 label 时只能说「不知道」，
# 这时候按「有安装记录就是自己的」处理 —— 别把升级变成「停别人的站」。
_container_ours() {
  need_cmd docker && docker container inspect "$SERVICE_NAME" >/dev/null 2>&1 || return 1
  local dir; dir="$(docker inspect -f '{{index .Config.Labels "simpleurl.dir"}}' "$SERVICE_NAME" 2>/dev/null || true)"
  [ -n "$dir" ] || return 0
  [ "$dir" = "$INSTALL_DIR" ]
}

stop_running() {
  # 容器名同样是全局唯一的，两份安装会撞在一起。容器带 labels（新版才打），
  # 认得出是本目录的就停；认不出又确实有安装记录在用它，就别顺手把别人的站停了。
  if [ "$RUNTIME" = "docker" ]; then
    _container_ours || { warn "容器 $SERVICE_NAME 不属于本目录，不动它"; return 0; }
    docker stop "$SERVICE_NAME" >/dev/null 2>&1 || true
  elif has_systemd_unit && need_cmd systemctl; then
    local SUDO=""; [ "$(id -u)" != "0" ] && need_cmd sudo && SUDO="sudo"
    $SUDO systemctl stop "$SERVICE_NAME" >/dev/null 2>&1 || true
  else
    # 三条线索合一：pid 文件、端口上是谁、命令行指向哪里。
    # 只看 pid 文件会出事：机器重启过、进程崩过、两份安装撞过同一个端口，
    # 那个号早被别人复用了 —— 不核对就 kill，等于把别人的进程顺手杀掉。
    # 只看端口也会出事：安装目录被删了而进程还活着，谁的记录都对不上，
    # 于是这个端口永久卡住，重装只能一遍遍 bind 失败。
    local pid="" pid_file="$INSTALL_DIR/simpleurl.pid"
    [ -f "$pid_file" ] && pid="$(cat "$pid_file" 2>/dev/null || echo "")"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && pid_in_this_dir "$pid"; then
      stop_pid "$pid"
    else
      if port_owner_ours "$PORT"; then
        stop_pid "$PORT_HOLDER_PID"
      elif [ -n "${PORT_ORPHAN_PID:-}" ]; then
        # 目录已经不在、记录也没了，但进程的 cwd 明明白白写着是这个目录 ——
        # 就是这套安装留下的孤儿。不杀掉，这个端口再也用不了。
        warn "端口 $PORT 上还留着本目录的孤儿进程 ${PORT_ORPHAN_PID}（安装目录已不在），先停掉它"
        stop_pid "$PORT_ORPHAN_PID"
      elif port_holder_pid "$PORT"; then
        warn "端口 $PORT 被 ${PORT_HOLDER_PID} 占着，那不是本目录的服务，不动它"
      fi
    fi
    rm -f "$pid_file"
  fi
  # 等它真的放开端口：紧接着就要覆盖源码了，进程还在读旧文件就是在给自己挖坑
  local i=0
  while [ "$i" -lt 10 ]; do
    port_in_use "$PORT" || break
    i=$((i + 1)); sleep 1
  done
}

# 停一个已经确认过归属的进程：先 TERM，等它自己收尾（关连接、写完 WAL），
# 等不到再 KILL。uvicorn 的收尾要一点时间，只发 TERM 就说「已停」不作数。
stop_pid() { # pid
  local pid="$1" n=0
  [ -n "$pid" ] || return 0
  kill "$pid" 2>/dev/null || true
  while [ "$n" -lt 10 ] && kill -0 "$pid" 2>/dev/null; do n=$((n + 1)); sleep 1; done
  kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
  sleep 1
  return 0
}

# 目标端口上是谁？只认「证据」，不认「记录」：
#   · pid 文件里记着它，且命令行 / cwd 指向本目录（同一个服务）
#   · 标记文件里记着「这套安装用这个端口」，而占用者的命令行或 cwd 指向本目录
# 反例（都是踩出来的）：
#   · 两份安装用同一个端口：B 的 pid 文件里那个号早被 A 重启后复用了，
#     只认 pid 文件就会判成自己人，B 一停把 A 干掉了；
#   · 安装目录被 rm -rf 掉了、进程还活着：cwd 与 pid 文件都对不上，谁都不敢动，
#     于是那个端口永久卡住，重装只能一遍遍 bind 失败。
# 这个函数只负责「认」，不动手 —— 动手的是 stop_pid。
PORT_ORPHAN_PID=""
port_owner_ours() { # port
  PORT_ORPHAN_PID=""
  local p="$1" pid cwd cmd rec_port raw_cwd
  port_holder_pid "$p" || return 1
  pid="$PORT_HOLDER_PID"
  [ -n "$pid" ] || return 1
  [ -d "/proc/$pid" ] || return 1
  cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null || true)"
  cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
  # 命令行里得真有 uvicorn / start.sh：pid 文件里的号可能早被别的程序复用。
  # 不需要额外的命令行过滤 —— 认不认得出来，全靠下面「参数或 cwd 指向本目录」，
  # 一条命令只要不是从这个目录起的，就对不上。（之前这里还有一道「必须是
  # uvicorn」的闸，两边都要过才算自己人，多一道只会让孤儿认不出来。）

  # 命令行 / cwd 指向本目录 —— 这是最硬的证据，跟有没有记录无关。
  # 先把它判掉，否则「目录被删了」那条永远轮不到：没了记录的孤儿正是靠
  # cwd 前缀带 deleted 后缀才认得出来的。
  case "$cwd" in
    "$INSTALL_DIR"|"$INSTALL_DIR"/*) : ;;
    *) case " $cmd " in *"$INSTALL_DIR"*) : ;; *) cwd="" ;; esac ;;
  esac
  if [ -n "$cwd" ] && [ "$cwd" = "$INSTALL_DIR" ]; then
    return 0
  fi
  rec_port="$(sed -n 's/^port=//p' "$MARK_FILE" 2>/dev/null | head -1)"
  if [ -f "$INSTALL_DIR/simpleurl.pid" ] && [ "$(cat "$INSTALL_DIR/simpleurl.pid" 2>/dev/null)" = "$pid" ] \
     && [ -n "$cwd" ]; then
    return 0
  fi
  if [ -n "$rec_port" ] && [ "$p" = "$rec_port" ]; then
    case "$cmd" in
      *"$INSTALL_DIR"*) return 0 ;;
      *) : ;;   # 参数里没有路径（cd 之后启动的那种）：再看 cwd
    esac
    [ -n "$cwd" ] && [ "$cwd" = "$INSTALL_DIR" ] && return 0
  fi
  # 目录已经不在了（rm -rf 掉了安装目录但进程还活着）：cwd 会带上 deleted 后缀
  raw_cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null || true)"
  case "$raw_cwd" in
    "$INSTALL_DIR"*) PORT_ORPHAN_PID="$pid"; return 1 ;;
  esac
  return 1
}

next_free_port() { # start —— 往后找 40 个，都没有就返回空
  local p
  for p in $(seq $(( $1 + 1 )) $(( $1 + 40 ))); do
    port_in_use "$p" || { printf '%s' "$p"; return 0; }
  done
  printf ''
}

check_port_free() {
  # 换壳重跑的那一轮：端口在 A 轮已经定过，别再「自动换个口」——
  # 同一件事两次给不同结论，比只给一次错的还糟。
  [ "${SIMPLEURL_PORT_FIXED:-n}" = "y" ] && return 0
  port_in_use "$PORT" || return 0
  local who=""
  if need_cmd ss; then who="$(ss -lntp 2>/dev/null | awk -v p=":$PORT" '$4 ~ p"$" {print $NF; exit}')"; fi
  # 占着端口的就是这套安装的进程（重装 / 升级时的常态）：这是自己人，
  # 不算冲突。真正要拦的是「别人的服务」——这时候不能默认硬上，也不能自作主张 kill。
  if port_owner_ours "$PORT"; then
    ok "端口 $PORT 由本目录的 SimpleURL 在用（重装时会先停掉）"
    return 0
  fi
  if [ -n "${PORT_ORPHAN_PID:-}" ]; then
    warn "端口 $PORT 上是本目录留下的孤儿进程 ${PORT_ORPHAN_PID}（安装记录已不在），安装时会先停掉它"
    return 0
  fi
  # 说清「被谁占着」。只报一句「被占用」，人就得自己去 ps 里翻 —— 而真正常见的
  # 情况是「这台机器上还装着另一份 SimpleURL」，那时候该做的是别装第二份，
  # 或者换个端口，而不是照着「自动换口」的提示再装出一个自己都不记得的站。
  local holder_hint=""
  if port_holder_pid "$PORT"; then
    holder_hint="（PID ${PORT_HOLDER_PID}：$(tr '\0' ' ' < "/proc/$PORT_HOLDER_PID/cmdline" 2>/dev/null | cut -c1-80)）"
  fi
  warn "端口 $PORT 被占用${who:+：$who}$holder_hint"
  local alt; alt="$(next_free_port "$PORT")"
  # 非交互（CI / 面板 / curl | bash --yes）时问不了人，也别硬撞：自己换个空的。
  # 但显式给了端口（--port / PORT=）就得照办：那是在说「我就要这个口」，
  # 而屏上的摘要、防火墙规则、反代上游都是照它来的。悄悄换掉最糟——
  # 装完一切看着正常，站点却在另一个端口上。
  if [ "$PORT_EXPLICIT" = "y" ]; then
    warn "仍按你指定的 $PORT 继续；启动会失败就停下来报错，不会偷偷换口"
    return 0
  fi
  if [ "$INTERACTIVE" = "0" ] || [ -z "$TTY" ]; then
    if [ -n "$alt" ]; then PORT="$alt"; ok "自动改用空闲端口 $PORT"
    else warn "附近端口都占着，仍按 $PORT 继续（大概率起不来）"; fi
    return 0
  fi
  ask "换个端口（留空${alt:+用 $alt}）" "$alt"
  [ -n "$ANSWER" ] && PORT="$ANSWER"
  return 0
}

wait_health() { # [max]
  local i=0 max="${1:-60}" code="000"
  step "等待服务就绪"
  if ! need_cmd curl; then sleep 3; return 0; fi
  while [ "$i" -lt "$max" ]; do
    code="$(curl -s -o /dev/null -w '%{http_code}' -m 2 "http://127.0.0.1:${PORT}/healthz" 2>/dev/null || echo 000)"
    [ "$code" = "200" ] && { ok "健康检查通过"; return 0; }
    i=$((i + 1)); printf "%s.%s" "$DIM" "$RST"; sleep 1
  done
  printf "\n"
  warn "健康检查未通过（HTTP $code），最近日志："
  if [ "$RUNTIME" = "docker" ]; then docker logs --tail 20 "$SERVICE_NAME" 2>&1 | sed 's/^/    /'
  else tail -n 20 "$LOG_FILE" 2>/dev/null | sed 's/^/    /'; fi
  return 1
}

# 健康检查只证明「进程活着」。装完能不能用，还得真打一遍业务接口：
# 登录拿签名 cookie、用凭据建一条短链、访问它拿到跳转、删掉善后。
# 这一步抓的是健康检查永远抓不到的事——口令写坏、CSRF 中间件把接口拦死、
# 短码生成炸了。任一条不过都不算安装成功。
#
# 两个坑记在这里：
#  · 登录接口本身也在 CSRF 校验范围内。跳过去直接 POST 会拿到 403，
#    看上去像「密码不对」，能把人带偏半小时。必须先 GET 首页拿 su_csrf。
#  · 验收流程里每一步都得能被后面的收尾引用（登录态 jar、建出了哪个码），
#    所以状态放在全局而不是全靠 local —— `local` + `set -u` 在 RETURN trap
#    里访问会直接 unbound variable。
VERIFY_SESSION_DIR=""
VERIFY_CODE=""
VERIFY_TARGET="${VERIFY_TARGET:-}"   # 仅用于自测：换一个验收要创建的短链目标
SCRIPT_REEXEC="${SCRIPT_REEXEC:-n}"   # 已为「换新脚本」重跑过一次，防无限自举
SCRIPT_FETCHED="n"                    # 本轮是否已经尝试过取远端脚本

verify_cleanup() {
  local rc=$?
  if [ -n "${VERIFY_SESSION_DIR:-}" ]; then
    rm -rf "$VERIFY_SESSION_DIR" 2>/dev/null || true
    VERIFY_SESSION_DIR=""
  fi
  return $rc
}

# 验收用的目标地址。默认打 example.com；离线机器 / DNS 被劫持的内网机器上
# 换成 127.0.0.1 也行，但 SSRF 防护会拦内网地址 —— 那种情况用
# `--verify-target <任意外网地址>` 或跳过验收（--no-health）更省事。
verify_target() {
  printf '%s' "${VERIFY_TARGET:-https://example.com/simpleurl-install-check}"
}

verify_end_to_end() {
  if [ "$SKIP_HEALTH" = "y" ] || ! need_cmd curl; then return 0; fi
  local how="管理员凭据"
  [ "$ACCESS_MODE" = "open" ] && how="管理员凭据（兼测访客能不能建）"
  step "验收：用$how真建一条短链、点开它、再删掉"

  local base="http://127.0.0.1:${PORT}"
  local jar csrf login resp code hop target
  VERIFY_CODE=""
  target="$(verify_target)"
  # 整个流程共用一份 cookie 罐，收尾在 verify_cleanup 里统一删
  VERIFY_SESSION_DIR="$(mktemp -d 2>/dev/null || { mkdir -p "/tmp/.simpleurl-verify.$$" && printf '%s' "/tmp/.simpleurl-verify.$$"; })"
  jar="$VERIFY_SESSION_DIR/cookies"
  trap verify_cleanup EXIT
  : > "$jar"

  curl -s -m 8 -o /dev/null -c "$jar" "$base/" 2>/dev/null || true
  csrf="$(awk '/su_csrf/{print $7}' "$jar" 2>/dev/null | head -1)"
  if [ -z "$csrf" ]; then
    warn "拿不到 CSRF 令牌（首页没下发 su_csrf），站点可能没正常起来"
    return 1
  fi

  login="$(curl -s -m 8 -o /dev/null -w '%{http_code}' -b "$jar" -c "$jar" \
      -H 'Content-Type: application/json' -H "Origin: $base" -H "X-CSRF-Token: $csrf" \
      -d "{\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PASS\"}" \
      "$base/api/admin/login" 2>/dev/null || echo 000)"
  if [ "$login" != "200" ]; then
    warn "管理员登录验收失败（HTTP $login）：后台密码进不去，等于没装好"
    return 1
  fi
  ok "管理员登录通过"

  resp="$(curl -s -m 10 -b "$jar" -c "$jar" \
      -H 'Content-Type: application/json' -H "Origin: $base" -H "X-CSRF-Token: $csrf" \
      -d "{\"target\":\"$target\",\"fetch_title\":false}" \
      "$base/api/links" 2>/dev/null || echo '')"
  code="$(printf '%s' "$resp" | sed -n 's/.*"code": *"\([^"]*\)".*/\1/p' | head -1)"
  if [ -z "$code" ]; then
    warn "建链验收失败，接口返回：$(printf '%s' "$resp" | head -c 160)"
    return 1
  fi
  VERIFY_CODE="$code"
  ok "建链通过（/$code）"

  # 短链的命根子是「点得开」：刚生成的码必须给出 3xx，而不是 404 / 500
  hop="$(curl -s -m 8 -o /dev/null -w '%{http_code}' "$base/$code" 2>/dev/null || echo 000)"
  case "$hop" in
    301|302|303|307|308) ok "跳转通过（HTTP $hop）" ;;
    *) warn "短链 /$code 跳转验收失败（HTTP $hop）"; return 1 ;;
  esac

  # 清场：验收造出来的这条不能留在用户的库里
  local del
  del="$(curl -s -m 8 -o /dev/null -w '%{http_code}' -b "$jar" -X DELETE -H "Origin: $base" \
        -H "X-CSRF-Token: $csrf" "$base/api/links/$code" 2>/dev/null || echo 000)"
  case "$del" in
    2*) ok "验收数据已清理（/$code）"; VERIFY_CODE="" ;;
    *)  warn "验收用的短链 /$code 没能删掉（HTTP $del），可到后台手动删" ;;
  esac

  # 开放模式下再验一条：访客能不能建。这条最容易在权限改造后悄悄坏掉——
  # 管理员一路畅通，访客却被 401 挡住，装的人要等真有人用才发现
  if [ "$ACCESS_MODE" = "open" ]; then
    local anon_jar anon_csrf resp2 code2
    anon_jar="$VERIFY_SESSION_DIR/anon"; : > "$anon_jar"
    curl -s -m 8 -o /dev/null -c "$anon_jar" "$base/" 2>/dev/null || true
    anon_csrf="$(awk '/su_csrf/{print $7}' "$anon_jar" 2>/dev/null | head -1)"
    if [ -n "$anon_csrf" ]; then
      resp2="$(curl -s -m 10 -b "$anon_jar" -c "$anon_jar" \
          -H 'Content-Type: application/json' -H "Origin: $base" -H "X-CSRF-Token: $anon_csrf" \
          -d "{\"target\":\"$target\",\"fetch_title\":false}" \
          "$base/api/links" 2>/dev/null || echo '')"
      code2="$(printf '%s' "$resp2" | sed -n 's/.*"code": *"\([^"]*\)".*/\1/p' | head -1)"
      if [ -n "$code2" ]; then
        ok "访客建链通过（/$code2）"
        # 访客建的东西要用**访客自己的钥匙**删：管理员那把开的是 /api/links/<code>，
        # 访客手里只有回执（claim_token），对应 /api/links/<code>/self。
        # 之前这里拿管理员接口删访客的码，开放模式下必然 401 —— 屏幕上多一句
        # 「没能删掉」，而验收实际上每装一次就往用户库里塞一条垃圾。
        tok2="$(printf '%s' "$resp2" | sed -n 's/.*"claim_token": *"\([^"]*\)".*/\1/p' | head -1)"
        del2="$(curl -s -m 8 -o /dev/null -w '%{http_code}' -b "$anon_jar" -X DELETE \
              -H "Origin: $base" -H "X-CSRF-Token: $anon_csrf" -H "X-Claim-Token: $tok2" \
              "$base/api/links/$code2/self?t=$tok2" 2>/dev/null || echo 000)"
        case "$del2" in
          2*) ok "访客验收数据已清理（/$code2）" ;;
          *)  warn "访客验收用的短链 /$code2 没能删掉（HTTP $del2），可到后台手动删" ;;
        esac
      else
        warn "开放模式下访客建链失败，接口返回：$(printf '%s' "$resp2" | head -c 160)"
        return 1
      fi
    fi
  fi
  return 0
}

# ---------------------------------------------------------------- 反向代理
# HTTPS 不该让用户自己拼 nginx 配置：短链发出去就是 https，配错一次等于断链。
# 这里按检测到的 Web 服务器生成一份能直接用的配置，并给出 certbot 的后续命令。
proxy_available() { need_cmd nginx || have_caddy || need_cmd apache2 || need_cmd httpd; }
have_caddy() { need_cmd caddy || [ -d /etc/caddy ]; }
have_nginx() { need_cmd nginx || [ -d /etc/nginx ]; }

write_proxy_config() { # domain
  local domain="$1" dir="$INSTALL_DIR/.proxy"
  mkdir -p "$dir"
  local up="127.0.0.1:$PORT"

  if have_caddy; then
    cat > "$dir/Caddyfile" <<EOF
# Caddy 自动申请并续期证书，这条就是全部配置
$domain {
	encode gzip zstd
	reverse_proxy $up {
		header_up X-Real-IP {remote_host}
	}
}
EOF
    PROXY_FILE="$dir/Caddyfile"; PROXY_KIND="caddy"
    printf "  %s\n" "把这段并入 /etc/caddy/Caddyfile，然后：systemctl reload caddy"
    return 0
  fi

  if have_nginx; then
    cat > "$dir/$domain.conf" <<EOF
server {
    listen 80;
    server_name $domain;

    # 短链要能被微信 / QQ 抓预览卡片，这几类跨站静态资源放开
    location /static/ { proxy_pass http://$up; proxy_set_header Host \$host; }

    location / {
        proxy_pass http://$up;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 60s;
    }
}
EOF
    PROXY_FILE="$dir/$domain.conf"; PROXY_KIND="nginx"
    printf "  %s\n" "配置文件：$PROXY_FILE"
    printf "  %s\n" "sudo cp $dir/$domain.conf /etc/nginx/conf.d/ && sudo nginx -t && sudo systemctl reload nginx"
    printf "  %s\n" "然后签证书：sudo certbot --nginx -d $domain"
    return 0
  fi

  if need_cmd apache2 || need_cmd httpd; then
    cat > "$dir/$domain.conf" <<EOF
<VirtualHost *:80>
    ServerName $domain
    ProxyPreserveHost On
    ProxyPass / http://$up/
    ProxyPassReverse / http://$up/
    RequestHeader set X-Forwarded-Proto "http"
</VirtualHost>
EOF
    PROXY_FILE="$dir/$domain.conf"; PROXY_KIND="apache"
    printf "  %s\n" "sudo cp $dir/$domain.conf /etc/apache2/sites-available/ && sudo a2ensite $domain && sudo systemctl reload apache2"
    printf "  %s\n" "然后签证书：sudo certbot --apache -d $domain"
    return 0
  fi

  PROXY_FILE=""; PROXY_KIND=""
  return 1
}

# ---------------------------------------------------------------- 回滚
ROLLED_BACK="n"

rollback_or_die() { # message
  local msg="$1"
  if [ -n "$PREV_BACKUP" ] && [ -d "$PREV_BACKUP" ]; then
    warn "正在回滚到升级前的状态：$PREV_BACKUP"
    rm -rf "${INSTALL_DIR:?}/.rollback"; mkdir -p "$INSTALL_DIR/.rollback"
    local f
    for f in simpleurl.db simpleurl.db-wal simpleurl.db-shm .env; do
      [ -f "$PREV_BACKUP/$f" ] && cp -a "$PREV_BACKUP/$f" "$INSTALL_DIR/.rollback/" 2>/dev/null || true
    done
    if [ -f "$INSTALL_DIR/.rollback/simpleurl.db" ]; then
      rm -f "$INSTALL_DIR/simpleurl.db" "$INSTALL_DIR/simpleurl.db-wal" "$INSTALL_DIR/simpleurl.db-shm"
      cp -a "$INSTALL_DIR/.rollback/simpleurl.db"* "$INSTALL_DIR/" 2>/dev/null || true
    fi
    rm -rf "$INSTALL_DIR/.rollback"
    if [ "$RUNTIME" = "docker" ] && [ -n "$PREV_IMAGE" ]; then
      docker rm -f "$SERVICE_NAME" >/dev/null 2>&1 || true
      docker run -d --name "$SERVICE_NAME" --restart unless-stopped \
        -p "${PORT}:8000" -v "${SERVICE_NAME}-data:/data" \
        "$PREV_IMAGE" >/dev/null 2>&1 || true
    fi
    ROLLED_BACK="y"
    die "$msg（已回滚数据与配置）"
  fi
  if [ -d "$INSTALL_DIR/.backup" ]; then
    local last; last="$(ls -1d "$INSTALL_DIR"/.backup/*/ 2>/dev/null | sort | tail -1)"
    [ -n "$last" ] && die "$msg（数据在 ${last%/}，可 simpleurl restore 手动恢复）"
  fi
  die "$msg"
}

# ---------------------------------------------------------------- 脚本自更新
# 安装目录里那份 install.sh 是装的那一刻的版本。如果升级只换 app/，机器上永远
# 跑着旧脚本：后来修掉的坑一个都用不上，而「修脚本」只能靠人再手敲一遍 curl。
# 所以升级时顺手把远端脚本也取一份，取回来先 bash -n 验语法，验过才用。
SCRIPT_URL_HINT=""   # 取到的远端脚本路径（空 = 没取到，沿用本地那份）
fetch_latest_script() { # 输出可执行的脚本路径，取不到就输出空
  local dir="$INSTALL_DIR/.stage-script" tmp urls=() out
  rm -rf "$dir"; mkdir -p "$dir"
  out="$dir/install.sh"
  if is_cnb; then
    urls+=("$GIT_HOST/$REPO_SLUG/-/git/raw/$REPO_BRANCH/install.sh")
    [ -n "${CNB_TOKEN:-}" ] && urls+=("$API_HOST/$REPO_SLUG/-/git/raw/$REPO_BRANCH/install.sh")
    [ "$API_HOST" != "$GIT_HOST" ] && urls+=("$API_HOST/$REPO_SLUG/-/git/raw/$REPO_BRANCH/install.sh")
    urls+=("$GIT_HOST/$REPO_SLUG/-/raw/$REPO_BRANCH/install.sh")
  else
    urls+=("$GIT_HOST/$REPO_SLUG/-/raw/$REPO_BRANCH/install.sh")
  fi
  urls+=("https://raw.githubusercontent.com/${REPO_SLUG}/$REPO_BRANCH/install.sh")
  for tmp in ${urls[@]+"${urls[@]}"}; do
    if fetch "$tmp" "$out" >/dev/null 2>&1 && is_shell_script "$out"; then
      bash -n "$out" 2>/dev/null && { printf '%s' "$out"; return 0; }
      warn "远端脚本语法检查没过，继续用本地这份"
    fi
    rm -f "$out"
  done
  printf ''
}

is_shell_script() { # file：认得 shebang 才行，CNB 网页路由会把 HTML 当脚本喂回来
  [ -s "$1" ] || return 1
  head -c 2 "$1" | grep -q '<' && return 1
  head -1 "$1" | grep -qE '^#!.*(bash|sh)'
}

# 用远端那份脚本替换安装目录里的自己。
#
# 以前安装目录里的 install.sh 从来没人更新：装的那天是哪一版，以后就一直是
# 哪一版。于是早装的机器永远拿旧脚本去升代码，新脚本里的修法（端口探测、
# 停服校验、strict 体检）它一个都用不上 —— 恰恰是「装得越早越修不到 bug」。
#
# 判据三条，全过才落盘：语法能被 bash 解析、认得 shebang、版本不更旧。
# 版本更旧就不换，不然一次误操作就能把脚本回退成旧的。
self_update_script() {
  local pending="$INSTALL_DIR/.install.sh.pending" target="$INSTALL_DIR/install.sh"
  [ "$SELF_UPDATE_PENDING" = "y" ] || return 0
  if [ ! -s "$pending" ]; then
    SELF_UPDATE_PENDING="n"; return 0
  fi
  if ! bash -n "$pending" 2>/dev/null; then
    warn "新脚本语法检查没过，安装目录里继续用旧脚本"
    rm -f "$pending"; SELF_UPDATE_PENDING="n"; return 0
  fi
  if ! is_shell_script "$pending"; then
    warn "新脚本不像 shell 脚本，不落盘"
    rm -f "$pending"; SELF_UPDATE_PENDING="n"; return 0
  fi
  if [ -f "$target" ] && version_lt "$(script_version "$pending")" "$(script_version "$target")"; then
    warn "新脚本版本更低，保留安装目录里那份"
    rm -f "$pending"; SELF_UPDATE_PENDING="n"; return 0
  fi
  if ! mv -f "$pending" "$target" 2>/dev/null; then
    warn "脚本没能替换（权限？），下次 upgrade 会自动重试"
    SELF_UPDATE_PENDING="n"; return 0
  fi
  chmod +x "$target" 2>/dev/null || true
  ok "安装目录里的脚本已更新（v$(script_version "$target")，以后 simpleurl upgrade 都用它）"
  SELF_UPDATE_PENDING="n"
}

# ---------------------------------------------------------------- simpleurl 命令
# 生成管理命令。它自己读 MARK_FILE 决定运行方式，所以脚本挪走、升级过
# 也不会跑到旧逻辑上。
install_cli() {
  local bindir="/usr/local/bin" SUDO=""
  if [ "$(id -u)" != "0" ]; then
    if need_cmd sudo && sudo -n true 2>/dev/null; then SUDO="sudo"
    else bindir="$HOME/.local/bin"; mkdir -p "$bindir"; fi
  fi
  [ -n "$SUDO" ] && $SUDO mkdir -p "$bindir" || mkdir -p "$bindir"
  local cli="$bindir/simpleurl"
  # heredoc 用 <<'QUOTED'：正文一个字节都不展开，`$` 和反引号都按字面写。
  #
  # 以前这里是 <<EOF 加「每个 $ 都要转义、正文不许出现反引号」的约定。约定
  # 守不住：`version` 那一行里嵌套的 $(cat app/__init__.py | sed ...) 装了两次
  # 反斜杠还是被外层吃掉，落盘的命令里那个子命令整段消失，于是 simpleurl version
  # 永远打「代码 」后面空白；顺带还有一处把 heredoc 定界符写成了 $USAGE。
  # 少一次展开就少一类 bug：安装目录路径这类真正要展开的，就在正文里另写一遍。
  # 正文一个字节都不展开（<<'SU_CLI_EOF'）：模板里的 $ 与反引号按字面写。
  # 装的时候才知道的值，用 __SU_*__ 占位，渲染完再 sed 替换。
  local rendered="$cli.render"
  ${SUDO} tee "$rendered" >/dev/null <<'SU_CLI_EOF'
#!/usr/bin/env bash
# SimpleURL 管理命令（由 install.sh __SU_SCRIPT_VERSION__ 生成；__SU_*__ 在安装时被替换成真实值）
set -uo pipefail
DIR="__SU_DIR__"
NAME="__SU_NAME__"
MARK="$DIR/.simpleurl-managed"
G=$'\033[32m'; R=$'\033[31m'; D=$'\033[2m'; N=$'\033[0m'

# 运行方式以安装时写的标记为准，命令本身不再带上过期信息
meta() { sed -n "s/^$1=//p" "$MARK" 2>/dev/null | head -1; }
RUNTIME="$(meta runtime)"
PORT="$(meta port)"
CODE_VER="$(meta code_version)"
SCRIPT_VER="__SU_SCRIPT_VERSION__"
URL_HINT="$(grep -E '^PUBLIC_URL=' "$DIR/.env" 2>/dev/null | head -1 | cut -d= -f2-)"
[ -n "$PORT" ] || PORT=8000

say() { printf '%s\n' "$*"; }
warn() { printf "%s!%s %s\n" "$R" "$N" "$*"; }

running() {
  if [ "$RUNTIME" = "docker" ]; then
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$NAME"
  elif [ -f "/etc/systemd/system/$NAME.service" ] && command -v systemctl >/dev/null 2>&1; then
    systemctl is-active --quiet "$NAME"
  else
    [ -f "$DIR/simpleurl.pid" ] && kill -0 "$(cat "$DIR/simpleurl.pid" 2>/dev/null)" 2>/dev/null
  fi
}

site_url() {
  if [ -n "$URL_HINT" ]; then printf '%s' "$URL_HINT"; return; fi
  local ip; ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  printf 'http://%s:%s' "${ip:-127.0.0.1}" "$PORT"
}

do_start() {
  if running; then say "已在运行"; return 0; fi
  if [ "$RUNTIME" = "docker" ]; then
    docker start "$NAME" >/dev/null && say "已启动容器 $NAME"; return 0
  elif [ -f "/etc/systemd/system/$NAME.service" ] && command -v systemctl >/dev/null 2>&1; then
    sudo systemctl start "$NAME" && say "已启动 systemd 服务 $NAME"; return 0
  fi
  setsid nohup "$DIR/start.sh" >>"$DIR/simpleurl.log" 2>&1 </dev/null &
  local pid=$!
  echo $pid > "$DIR/simpleurl.pid"
  # 起没起来要说实话：start.sh 里的 exec 失败时这一步两秒内就退了，
  # 屏幕上却只留一句「已后台启动」，人要去翻日志才知道根本没跑起来。
  # 等两秒：uvicorn 起得来就还活着，起不来这个 pid 两秒内已经退了。
  # 不能用 kill -0 循环 —— 循环条件是「还活着就等」，进程一退就提前跳出，
  # 那样只能抓到「起不来」，抓不到「起来了」。
  sleep 2
  if kill -0 "$pid" 2>/dev/null; then
    say "已后台启动，PID $pid"
  else
    warn "进程立刻退出了（PID $pid），最近日志："
    tail -n 12 "$DIR/simpleurl.log" 2>/dev/null | sed 's/^/    /'
    return 1
  fi
}

do_stop() {
  if [ "$RUNTIME" = "docker" ]; then
    docker stop "$NAME" >/dev/null 2>&1 && say "已停止容器 $NAME" || say "容器未运行"
  elif [ -f "/etc/systemd/system/$NAME.service" ] && command -v systemctl >/dev/null 2>&1; then
    sudo systemctl stop "$NAME" >/dev/null 2>&1 && say "已停止 $NAME"
  elif [ -f "$DIR/simpleurl.pid" ]; then
    local pid; pid="$(cat "$DIR/simpleurl.pid" 2>/dev/null)"
    # 光看 pid 文件不够：号可能早被别的程序复用了（机器重启过、进程崩过、
    # 两份安装撞过同一个号）。先确认命令行或工作目录真指向本目录，
    # 认不出来就只删文件、不动进程 —— 宁可多起一个，也不误杀别人的站。
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && pid_is_ours "$pid"; then
      kill "$pid" 2>/dev/null || true
      # 只发 TERM 就在屏幕上说「已停止」是不作数的：uvicorn 收尾要一点时间，
      # 卡住的进程还会一直占着端口。等不到就上 KILL，还不行就明说。
      local n=0
      while [ "$n" -lt 10 ] && kill -0 "$pid" 2>/dev/null; do n=$((n + 1)); sleep 1; done
      if kill -0 "$pid" 2>/dev/null; then
        kill -9 "$pid" 2>/dev/null || true
        sleep 1
      fi
      if kill -0 "$pid" 2>/dev/null; then warn "进程 $pid 没能停掉（权限？）"
      else say "已停止（PID $pid）"; fi
    elif [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      # 进程活着，但命令行和目录都对不上：那是别人的（pid 被复用）。
      # 说清楚，别让人以为「stop 了但没停」。
      warn "记录里的进程 $pid 不属于本目录（pid 被复用？），未动它；pid 文件已清理"
    else
      say "记录里的进程已不在（pid 文件过期，已清理）"
    fi
    rm -f "$DIR/simpleurl.pid"
  else
    say "未在运行"
  fi
  # 停完等端口真的放开。do_stop 返回的那一刻如果端口还占着，紧接着的
  # do_start 会 bind 失败，而屏幕上只留下一句「已启动」。
  wait_port_free || warn "端口 $PORT 仍被占用，启动可能失败（simpleurl log 看详情）"
}

# 生成的命令行是 set -uo pipefail 跑的。下面每个 $ 都必须带反斜杠：
# 这整段在安装时被套进外层 heredoc 展开一次，少了反斜杠就当场展开，
# 到了运行时只剩一个空变量名，报 unbound variable。
# 端口探测：ss / lsof / netstat 都没有时，退回内核的 proc/net/tcp。
# 以前三样都没有就 return 0（当成已放开），于是 restart 在旧进程还占着
# 端口时就去启新的 —— 新进程 bind 失败，屏幕上却是「已启动」。
port_listening() {
  local hex f
  # 同样四条路叠加、判「有」即算。elif 链只信 ss 时，ss 看不见的端口会被判成
  # 空闲，restart 于是在旧进程仍占着端口时抢跑，失败信息却指向别处。
  if command -v ss >/dev/null 2>&1; then
    ss -lnt 2>/dev/null | awk -v p=":$PORT" '$4 ~ p"$" {found=1} END{exit !found}' && return 0
  fi
  if command -v lsof >/dev/null 2>&1; then
    lsof -ti "tcp:$PORT" >/dev/null 2>&1 && return 0
  fi
  if command -v netstat >/dev/null 2>&1; then
    netstat -lnt 2>/dev/null | awk -v p=":$PORT" '$4 ~ p"$" {found=1} END{exit !found}' && return 0
  fi
  if [ -r /proc/net/tcp ]; then
    hex="$(printf '%04X' "$PORT" 2>/dev/null)"
    for f in /proc/net/tcp /proc/net/tcp6; do
      [ -r "$f" ] || continue
      awk -v h=":$hex" '$4 == "0A" && substr($2, length($2) - 4) == h {found=1} END{exit !found}' "$f" 2>/dev/null && return 0
    done
  fi
  (exec 3<>"/dev/tcp/127.0.0.1/$PORT") >/dev/null 2>&1 && return 0
  return 1
}

wait_port_free() {
  local i=0
  while [ "$i" -lt 10 ]; do
    port_listening || return 0
    i=$((i + 1)); sleep 1
  done
  return 1
}

# pid 文件里的那个号还属于本目录吗？只认命令行与工作目录两条证据。
# pid 文件本身不算证据：机器重启过、进程崩过、两份安装撞过同一个端口，
# 那个号都可能已经被别的程序复用 —— 拿它当「自己人」就会误杀。
pid_in_this_dir() { # pid
  local pid="$1"
  [ -n "$pid" ] && [ -d "/proc/$pid" ] || return 1
  tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q -F -- "$INSTALL_DIR" && return 0
  [ "$(readlink "/proc/$pid/cwd" 2>/dev/null)" = "$INSTALL_DIR" ] && return 0
  return 1
}

# 这个 pid 是不是本目录的服务？命令行里带 $DIR，或者 cwd 就是 $DIR。
# 只认这两条：认不出来宁可不停，也不误杀 —— pid 会被别的程序复用。
pid_is_ours() { # pid
  local pid="$1"
  [ -n "$pid" ] && [ -d "/proc/$pid" ] || return 1
  tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q -F -- "$DIR" && return 0
  [ "$(readlink "/proc/$pid/cwd" 2>/dev/null)" = "$DIR" ] && return 0
  return 1
}

health() {
  command -v curl >/dev/null 2>&1 || return 0
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:$PORT/healthz" 2>/dev/null || echo 000)"
  if [ "$code" = "200" ]; then printf "  健康检查 %s200 OK%s\n" "$G" "$N"
  else printf "  健康检查 %s%s%s\n" "$R" "$code" "$N"; fi
}

# 不带参数只打印当前密码，带 --reset 或直接给新密码才动手改。
# 原来不给参数就随机重置，而它是最常敲的一条：敲一下等于把现成的密码作废，
# 还得回头去翻刚滚过去的那行输出。所以默认改成只读。
show_password() {
  local user pass raw
  user="$(grep -E '^ADMIN_USERNAME=' "$DIR/.env" 2>/dev/null | head -1 | cut -d= -f2-)"
  pass="$(grep -E '^ADMIN_PASSWORD=' "$DIR/.env" 2>/dev/null | head -1 | cut -d= -f2-)"
  if [ -z "$pass" ]; then
    # 老安装只有 ADMIN_TOKEN：admin:password 分开写，纯口令则账号固定 admin
    raw="$(grep -E '^ADMIN_TOKEN=' "$DIR/.env" 2>/dev/null | head -1 | cut -d= -f2-)"
    case "$raw" in
      *:*) user="${user:-${raw%%:*}}"; pass="${raw#*:}" ;;
      "")  warn "找不到管理员凭据，看看 simpleurl config"; return 1 ;;
      *)   pass="$raw" ;;
    esac
  fi
  say "账号：${user:-admin}"
  say "密码：$pass"
  say "后台：$(site_url)/admin"
  say "（要换密码：simpleurl password --reset）"
}

# 改 .env 里的一行：有就替换、没有就追加，其余内容一字不动。
# 用 awk 而不是 sed 的原地编辑：BSD 与 GNU 的 -i 参数长得不一样，之前为此写过两套分支。
env_put() { # file key value
  local file="$1" key="$2" val="$3" tmp="$1.su-new"
  KEY="$key" VAL="$val" awk '
    BEGIN { k = ENVIRON["KEY"]; v = ENVIRON["VAL"]; done = 0 }
    index($0, k "=") == 1 { if (!done) { print k "=" v; done = 1 }; next }
    { print }
    END { if (!done) print k "=" v }
  ' "$file" > "$tmp" 2>/dev/null && mv "$tmp" "$file" && chmod 600 "$file" 2>/dev/null
}

set_password() {
  local pass="${1:-}" user
  if [ -z "$pass" ]; then
    pass="$(openssl rand -base64 32 2>/dev/null | tr -dc 'A-Za-z2-9' | cut -c1-16)"
  fi
  user="$(grep -E '^ADMIN_USERNAME=' "$DIR/.env" 2>/dev/null | head -1 | cut -d= -f2-)"
  user="${user:-admin}"
  env_put "$DIR/.env" ADMIN_USERNAME "$user"
  env_put "$DIR/.env" ADMIN_PASSWORD "$pass"
  # 改成账号密码以后，旧的独立令牌就没有存在意义了：留着等于多一把没人记得的钥匙
  env_put "$DIR/.env" ADMIN_TOKEN ""
  # 改完要拿新密码真登一次。这里以前改完就重启、屏幕上写「新密码：xxx」，
  # 至于它能不能进门全靠运气：.env 里上一次被写成空的 ADMIN_PASSWORD 时，
  # 站点启用的还是更早那份 ADMIN_TOKEN，新密码根本没人认。
  # 顺序：先重启（新密码已落盘）再验，验不过就把新密码留在屏幕上并报错。
  if [ "$RUNTIME" = "docker" ]; then
    # 容器启动参数里也带着凭据，只改 /data/.env 不够——重建一遍才真的生效
    docker exec "$NAME" sh -c 'cat > /data/.env && chmod 600 /data/.env' < "$DIR/.env" >/dev/null 2>&1 || true
    if ! docker run -d --name "$NAME" --restart unless-stopped \
          -p "${PORT}:8000" -v "${NAME}-data:/data" \
          -e "SITE_NAME=$(grep -E '^SITE_NAME=' "$DIR/.env" | head -1 | cut -d= -f2-)" \
          -e "PUBLIC_URL=$(grep -E '^PUBLIC_URL=' "$DIR/.env" | head -1 | cut -d= -f2-)" \
          -e "ACCESS_MODE=$(grep -E '^ACCESS_MODE=' "$DIR/.env" | head -1 | cut -d= -f2-)" \
          -e "SECRET_KEY=$(grep -E '^SECRET_KEY=' "$DIR/.env" | head -1 | cut -d= -f2-)" \
          -e "ADMIN_USERNAME=$user" -e "ADMIN_PASSWORD=$pass" \
          -e "DATABASE_URL=sqlite:////data/simpleurl.db" -e "PORT=8000" \
          -e "SIMPLEURL_ENV_FILE=/data/.env" \
          --label "simpleurl.managed=1" simpleurl:latest >/dev/null 2>&1; then
      warn "容器重建失败，新密码可能没生效：docker rm -f $NAME 后重跑 install.sh"
    fi
  else
    do_stop >/dev/null; sleep 1; do_start >/dev/null
  fi
  say "账号：$user"
  say "新密码：$pass"
  say "后台：$(site_url)/admin"
  say "（改完记得同步改你自己的密码管理器）"
  verify_login "$user" "$pass" || warn "新密码没能登录成功（旧密码可能还在生效）：看 simpleurl log"
}

# 拿账号密码真敲一次登录接口。和安装时的验收同一套判据：先取 CSRF，再 POST。
# 只报「改好了」却不验，等于把发现问题的时机推迟到用户被关在门外那一刻。
verify_login() { # user password
  # 没有 curl 就说「没验」，别装作验过了 —— 装完屏幕上那句「新密码已验证可用」
  # 是这个函数唯一的产出，它不能靠沉默来获得。
  if ! command -v curl >/dev/null 2>&1; then
    say "（没有 curl，跳过了新密码的登录验证）"
    return 0
  fi
  local user="$1" pass="$2" base jar csrf code
  base="http://127.0.0.1:$PORT"
  jar="$(mktemp 2>/dev/null || echo "/tmp/.su-login.$$")"
  : > "$jar"
  curl -s -m 8 -o /dev/null -c "$jar" "$base/" 2>/dev/null || true
  # cookie 文件是「7 个制表符分隔的字段 + 名字 + 值」：按字段取值，别数字段。
  # 以前写的是 awk 的 $7 —— 那恰好是值的位置，只因为常用的写法里第 6 个字段
  # （域）是空的。cookie 带上 Domain 或多一个属性时整行就错开一格，
  # 取到的是属性名而不是令牌，看起来像「站点没下发 CSRF」。
  csrf="$(awk -F'\t' '$6 == "su_csrf" {print $7; exit}' "$jar" 2>/dev/null)"
  if [ -z "$csrf" ]; then rm -f "$jar"; return 1; fi
  code="$(curl -s -m 8 -o /dev/null -w '%{http_code}' -b "$jar" -c "$jar" \
      -H 'Content-Type: application/json' -H "Origin: $base" -H "X-CSRF-Token: $csrf" \
      -d "{\"username\":\"$user\",\"password\":\"$pass\"}" "$base/api/admin/login" 2>/dev/null || echo 000)"
  rm -f "$jar"
  case "$code" in
    200) say "✓ 新密码已验证可用"; return 0 ;;
    *)   return 1 ;;
  esac
}

site_status() {
  if running; then printf "%s● 运行中%s\n" "$G" "$N"
  else printf "%s● 已停止%s\n" "$R" "$N"; fi
  health
  printf "  站点 %s\n  后台 %s/admin\n" "$(site_url)" "$(site_url)"
  if [ "$RUNTIME" = "docker" ]; then docker ps --filter "name=^$NAME$" --format '  容器 {{.Status}} · {{.Ports}}' 2>/dev/null || true; fi
  return 0
}

do_backup() {
  local out="${1:-$HOME/simpleurl-backup-$(date +%Y%m%d-%H%M%S).tar.gz}"
  mkdir -p "$(dirname "$out")"
  if [ "$RUNTIME" = "docker" ]; then
    docker run --rm -v "${NAME}-data:/data" -v "$(dirname "$out"):/backup" alpine \
      tar -czf "/backup/$(basename "$out")" -C /data . >/dev/null && say "已备份到 $out"
  else
    tar -czf "$out" -C "$DIR" simpleurl.db .env 2>/dev/null && say "已备份到 $out"
  fi
}

# 从备份里恢复数据。只覆盖数据库和配置，代码不动。
do_restore() {
  local src="${1:-}" tmp
  [ -n "$src" ] && [ -f "$src" ] || { warn "用法：simpleurl restore <备份文件.tar.gz>"; return 2; }
  if [ "$RUNTIME" = "docker" ]; then
    warn "容器模式请手动恢复：docker run --rm -v ${NAME}-data:/data -v $(dirname "$src"):/b alpine tar -xzf /b/$(basename "$src") -C /data"
    return 2
  fi
  tmp="$(mktemp -d)"
  tar -xzf "$src" -C "$tmp" 2>/dev/null || { warn "不是有效的备份包"; rm -rf "$tmp"; return 2; }
  do_stop >/dev/null
  [ -f "$tmp/simpleurl.db" ] && cp -a "$tmp/simpleurl.db" "$DIR/" && rm -f "$DIR/simpleurl.db-wal" "$DIR/simpleurl.db-shm"
  [ -f "$tmp/.env" ] && cp -a "$tmp/.env" "$DIR/"
  rm -rf "$tmp"
  do_start >/dev/null
  say "已从 $src 恢复，服务已重启"
}

# 不带参数就报状态：敲下去是想知道「现在怎么样」，不是想读一屏用法。
# 但它必须排在 * 分支之前——case 从上往下匹配，* 在前面就永远轮不到。
case "${1:-status}" in
  start)   do_start ;;
  stop)    do_stop ;;
  # 停完不能立刻起：uvicorn 放端口要一点时间，抢跑会 bind 失败，
  # 留下一个「restart 之后反而没了」的现场
  # do_stop 自己会等到端口放开（见上），这里不再重复等：重复一遍只是多花十秒
  restart) do_stop >/dev/null; do_start ;;
  status)  site_status ;;
  url)     site_url; echo ;;
  # 代码版本取标记文件里的 code_version：它由安装/升级流程写入，可靠。
  # 这里原来写的是 $(cat app/__init__.py | sed ...) —— 嵌套的 $( ) 在安装时
  # 被外层 heredoc 展开掉了（反斜杠只护住了最外层），落盘的命令行里那个子命令
  # 直接消失，于是 simpleurl version 永远打印「代码 」后面一片空白。
  version) say "install.sh $SCRIPT_VER（运行方式 $RUNTIME，代码 $CODE_VER）" ;;
  log)
    if [ "$RUNTIME" = "docker" ]; then docker logs --tail 100 -f "$NAME"
    else tail -n 100 -f "$DIR/simpleurl.log"; fi ;;
  password)
    # 不带参数 = 看密码（只读），--reset = 换成随机新密码
    case "${2:-}" in
      ""|--show) show_password ;;
      --reset)   set_password "" ;;
      *)         set_password "${2}" ;;
    esac ;;
  config)
    if [ "$RUNTIME" = "docker" ]; then
      docker exec "$NAME" cat /data/.env 2>/dev/null || cat "$DIR/.env.docker"
      say ""
      say "（Docker 模式改配置：docker exec -it $NAME vi /data/.env 后 simpleurl restart）"
    else
      "${EDITOR:-vi}" "$DIR/.env"
    fi ;;
  backup)  do_backup "${2:-}" ;;
  restore) do_restore "${2:-}" ;;
  upgrade)
    # 升级要连脚本一起换。否则装了 v2.2 的机器永远用 2.2 的脚本去升代码，
    # 新脚本里的修法自己用不上——上一轮的安装目录里躺着的就是旧脚本。
    # 这里同样是 case 分支而不是函数体，不用 local（用了当场 local: not in a function）
    script="$DIR/install.sh"
    if [ -f "$DIR/install.sh" ]; then
      newest="$DIR/install.sh.new"
      if [ -n "${SU_REMOTE_SCRIPT:-}" ] && [ -s "$SU_REMOTE_SCRIPT" ]; then
        if bash -n "$SU_REMOTE_SCRIPT" 2>/dev/null; then cp "$SU_REMOTE_SCRIPT" "$newest" 2>/dev/null || true; fi
      fi
      if [ -s "$newest" ]; then script="$newest"; fi
    fi
    if [ -f "$script" ]; then SIMPLEURL_HOME="$DIR" exec bash "$script" upgrade --yes
    else warn "缺少 $DIR/install.sh，请重新执行安装脚本"; exit 2; fi ;;
  doctor)
    if [ -f "$DIR/install.sh" ]; then exec bash "$DIR/install.sh" doctor
    else warn "缺少 $DIR/install.sh，请重新执行安装脚本"; exit 2; fi ;;
  uninstall)
    # 卸载会把 $DIR 整个删掉，跑在里面的 install.sh 也会一起消失；
    # 先拷一份到临时目录再跑，否则中途读不到脚本后半截
    if [ -f "$DIR/install.sh" ]; then
      # 这条命令是顶部 case 的一段，不是函数体：写 local 会当场报错
      tmp="$(mktemp -d 2>/dev/null || echo "/tmp/.su-uninstall.$$")"
      cp "$DIR/install.sh" "$tmp/install.sh" && chmod +x "$tmp/install.sh"
      # 带上 --force：外层已经确认过一次，拷出来这份不该再问第二遍
      # （原来这里不带，非交互调用永远卡在第二个反问上，还被报成「未知参数」）
      SIMPLEURL_HOME="$DIR" exec bash "$tmp/install.sh" uninstall --force
    else warn "缺少 $DIR/install.sh，请重新执行安装脚本"; exit 2; fi ;;
  "")      site_status ;;
  help|--help|-h|*)
    cat <<'\USAGE' | sed "s/^/  /"
SimpleURL 管理命令

simpleurl status              运行状态与健康检查
simpleurl start | stop | restart
simpleurl log                 跟踪日志
simpleurl url                 打印站点与后台地址
simpleurl version             脚本与代码版本
simpleurl password              看当前管理员密码（只读）
simpleurl password --reset      换成随机新密码
simpleurl password 新密码       设成指定的密码
simpleurl config              查看 / 编辑配置
simpleurl backup [路径]        备份数据库与配置
simpleurl restore <备份>       从备份恢复数据
simpleurl upgrade             拉新代码升级（失败自动回滚数据）
simpleurl doctor              环境体检
simpleurl uninstall           卸载
\USAGE
    ;;
esac
SU_CLI_EOF
  local esc_dir esc_name esc_ver
  esc_dir="$(printf '%s' "$INSTALL_DIR" | sed 's/[&|\\]/\\&/g')"
  esc_name="$(printf '%s' "$SERVICE_NAME" | sed 's/[&|\\]/\\&/g')"
  esc_ver="$(printf '%s' "$VERSION" | sed 's/[&|\\]/\\&/g')"
  $SUDO sed -i.su "s|__SU_DIR__|$esc_dir|g;s|__SU_NAME__|$esc_name|g;s|__SU_SCRIPT_VERSION__|$esc_ver|g" "$rendered"
  # 权限显式给 755：`cat > file` 沿用已存在文件的权限，而 mktemp / 某些 umask
  # 环境下会是 600 —— 装完 root 敲 simpleurl 能用（root 不看权限位），
  # 换成普通运维账号就 Permission denied，而且报错长得像「文件坏了」。
  $SUDO sh -c "cat '$rendered' > '$cli' && chmod 755 '$cli'"
  $SUDO rm -f "$rendered" "$rendered.su"
  # 安装目录里再留一份影子。机器上只装一份时用不到它，装了两份就是救命的：
  # /usr/local/bin/simpleurl 只有一个名字，装第二份会把第一份覆盖掉，于是
  # 第一份的 install.sh 再也找不到「属于自己的」命令行。有影子就够了，
  # forward_subcommand 优先用它 —— 这也是「这套安装的东西放自己家里」。
  $SUDO sh -c "cp -f '$cli' '$INSTALL_DIR/simpleurl' && chmod 755 '$INSTALL_DIR/simpleurl'" 2>/dev/null || true
  # 顺序很重要：先把这份命令落盘，再登记兄弟安装 —— 反过来会把「自己还没生成命令」
  # 那一刻的邻居名单当成最终答案，装第二份时写下的是一份空记录。
  record_siblings
  ok "管理命令已就位：$cli"
  case ":$PATH:" in *":$bindir:"*) : ;; *) warn "$bindir 不在 PATH，可用绝对路径 $cli" ;; esac
}

# 把「这台机器上还装着谁」记进 SIBLING_FILE。
# 两份安装共用一个命令名，后装的那份会把全局命令覆盖成自己的 —— 先装那份
# 从此只剩目录里的影子命令。卸载后装的那份时，靠这份记录把命令接回去。
record_siblings() {
  local d cmd
  local found=()
  for cmd in /usr/local/bin/simpleurl "$HOME/.local/bin/simpleurl" \
             "$HOME"/simpleurl/simpleurl; do
    [ -f "$cmd" ] || continue
    d="$(sed -n 's/^DIR="\(.*\)"$/\1/p' "$cmd" 2>/dev/null | head -1)"
    [ -n "$d" ] || continue
    [ "$d" = "$INSTALL_DIR" ] && continue
    [ -f "$d/.simpleurl-managed" ] && found+=("$d")
  done
  # 目录里那份影子命令也是线索：安装顺序反过来时（先装 A 后装 B），
  # 全局命令指向 B，A 只剩 /tmp/su-a/simpleurl 这一份 —— 而它同样记着 DIR。
  # /tmp、/opt、/srv、$HOME 下的一级目录，谁家都可能装 —— 所以从「全局命令指向
  # 哪儿」「记录里写过哪儿」反推候选目录，而不是满世界找标记文件。
  for cmd in $(ls -d /tmp/*/simpleurl /opt/*/simpleurl /srv/*/simpleurl \
                  "$HOME"/*/simpleurl 2>/dev/null); do
    [ -f "$cmd" ] || continue
    d="$(sed -n 's/^DIR="\(.*\)"$/\1/p' "$cmd" 2>/dev/null | head -1)"
    [ -n "$d" ] || continue
    [ "$d" = "$INSTALL_DIR" ] && continue
    [ -f "$d/.simpleurl-managed" ] && found+=("$d")
  done
  # 上一轮记下的也保留：命令被覆盖之后，那份记录可能是唯一线索
  for d in $(sed -n 's/^dir=//p' "$SIBLING_FILE" 2>/dev/null); do
    [ "$d" = "$INSTALL_DIR" ] && continue
    [ -f "$d/.simpleurl-managed" ] || continue
    case " ${found[*]-} " in *" $d "*) : ;; *) found+=("$d") ;; esac
  done
  : > "$SIBLING_FILE"
  chmod 600 "$SIBLING_FILE" 2>/dev/null || true
  for d in ${found[@]+"${found[@]}"}; do
    printf 'dir=%s\n' "$d" >> "$SIBLING_FILE"
  done
  [ ${#found[@]} -gt 0 ] && ok "顺带记下机器上另外的安装：${found[*]}"
  return 0
}

# ---------------------------------------------------------------- 收尾
summary() {
  local url admin_url
  if [ -n "$PUBLIC_URL" ]; then url="$PUBLIC_URL"; else url="http://$(detect_ip):$PORT"; fi
  admin_url="$url/admin"

  printf "\n%s%s 安装完成 %s%s（v%s · %s）%s\n" "$GREEN" "$BOLD" "$RST" "$DIM" "$VERSION" "$RUNTIME" "$RST"
  hr
  printf "  %s站点首页%s    %s%s%s\n" "$DIM" "$RST" "$BOLD" "$url" "$RST"
  printf "  %s管理后台%s    %s%s%s\n" "$DIM" "$RST" "$BOLD" "$admin_url" "$RST"
  printf "  %s管理员账号%s  %s\n" "$DIM" "$RST" "$ADMIN_USER"
  if [ -n "$ADMIN_PASS" ]; then
    printf "  %s管理员密码%s  %s%s%s\n" "$DIM" "$RST" "$YELLOW" "$ADMIN_PASS" "$RST"
  else
    printf "  %s管理员密码%s  %s未改动（simpleurl password 看，--reset 换新的）%s\n" "$DIM" "$RST" "$DIM" "$RST"
  fi
  printf "  %s访问模式%s    %s\n" "$DIM" "$RST" "$ACCESS_MODE"
  printf "  %s安装目录%s    %s\n" "$DIM" "$RST" "$INSTALL_DIR"
  hr
  printf "  %s后续管理（%ssimpleurl --help%s%s 看全部）%s\n" "$BOLD" "$DIM" "$RST" "$BOLD" "$RST"
  printf "    simpleurl status      看状态\n"
  printf "    simpleurl log         看日志\n"
  printf "    simpleurl upgrade     升级到最新代码\n"
  printf "    simpleurl backup      备份数据\n"
  printf "    simpleurl password    看管理员密码（--reset 换新的）\n"
  hr
  [ -n "$PREV_BACKUP" ] && printf "  %s上一版数据已备份到 %s（只留最近 5 份）%s\n" "$DIM" "$PREV_BACKUP" "$RST"
  # 升级 / 重装时把「这轮动了什么」说清楚：装的人最想知道的是这个，
  # 而不是再去 ls 一遍安装目录自己比对。
  if [ -n "${PREV_CODE_VERSION:-}" ] && [ "$PREV_CODE_VERSION" != "$CODE_VERSION" ]; then
    printf "  %s代码已更新 %s → %s%s\n" "$DIM" "$PREV_CODE_VERSION" "$CODE_VERSION" "$RST"
  elif [ "$SOURCE_REUSED" = "y" ]; then
    printf "  %s本轮代码未变动（只重装了依赖 / 重启了服务）%s\n" "$DIM" "$RST"
  fi
  printf "  %s密码请立刻抄走。之后可在后台「设置 → 管理员账号」改，改完重启不丢。%s\n" "$YELLOW" "$RST"
  [ -z "$PUBLIC_URL" ] && printf "  %s想让短链显示成自己的域名？在 %s/.env 里设 PUBLIC_URL=https://s.example.com 再 restart。%s\n" "$DIM" "$INSTALL_DIR" "$RST"
  if [ -n "${PROXY_FILE:-}" ] && [ -f "$PROXY_FILE" ]; then
    printf "  %s反向代理%s    %s（%s，照注释里的命令挂上去就生效）\n" "$DIM" "$RST" "$PROXY_FILE" "$PROXY_KIND"
  fi
  printf "  %s装完想自检：simpleurl doctor；出问题先看 simpleurl log。%s\n" "$DIM" "$RST"
  printf "  %sPowered by SimpleURL — https://cnb.cool/%s%s\n\n" "$DIM" "$REPO_SLUG" "$RST"
}

do_uninstall() {
  step "卸载 SimpleURL"
  if [ ! -f "$MARK_FILE" ]; then
    die "$INSTALL_DIR 不是本脚本装的目录，为安全起见不自动删"
  fi
  # --force 是「我知道自己在删什么」的显式确认。以前它只被解析、从不生效，
  # 非交互卸载永远卡在这个反问上 —— 用法说明里写着 --force，实际不能用。
  if [ "$ASSUME_YES" = "y" ]; then
    warn "按 --force 跳过确认，直接删除 $INSTALL_DIR"
  else
    ask_yes "确认删除 $INSTALL_DIR 与全部数据？（不可恢复）" "n"
    [ "$ANSWER" = "y" ] || die "已取消（非交互请加 --force）"
  fi
  if need_cmd docker && docker container inspect "$SERVICE_NAME" >/dev/null 2>&1; then
    docker rm -f "$SERVICE_NAME" >/dev/null 2>&1 || true
    docker volume rm "${SERVICE_NAME}-data" >/dev/null 2>&1 || true
    docker rmi simpleurl:latest >/dev/null 2>&1 || true
    ok "容器与数据卷已删除"
  fi
  SUDO=""
  [ "$(id -u)" != "0" ] && need_cmd sudo && SUDO="sudo"
  if need_cmd systemctl; then
    $SUDO systemctl stop "$SERVICE_NAME" >/dev/null 2>&1 || true
    $SUDO systemctl disable "$SERVICE_NAME" >/dev/null 2>&1 || true
    $SUDO rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
    $SUDO systemctl daemon-reload >/dev/null 2>&1 || true
  fi
  if [ -f "$INSTALL_DIR/simpleurl.pid" ]; then
    kill "$(cat "$INSTALL_DIR/simpleurl.pid" 2>/dev/null)" 2>/dev/null || true
  fi
  # 两件事一起做：只删指向本目录的那份全局命令（两份安装共用一个名字，
  # 删错就把隔壁断了）；同时记下隔壁那份（still_there），本目录删完要用它把
  # 全局命令接回去。以前这里只做第一件，本目录一删「隔壁在哪」就再也没线索了。
  #
  # 但「隔壁」不能只认全局命令那一条线索：后装的那份会把 /usr/local/bin/simpleurl
  # 覆盖成自己的，先装那份就只剩自己目录里的影子命令 —— 于是卸载后装的那份时，
  # 全局命令被删掉、先装那份却接不回来，机器上从此一条 simpleurl 都没有（站点还在跑）。
  # 所以有安装记录（.simpleurl-managed）的目录都算候选。
  #
  # 两个细节都是踩过的坑：
  #  · `[ -f ] || continue` 不能用 —— bash 会把整段 for 当函数体跳过，第二个
  #    路径压根没被检查过，于是永远只删 /usr/local/bin 那一份；
  #  · `rm && x=y` 不能当分支尾巴 —— 它是「最后一条命令」，false 会让 set -e
  #    直接退出，卸载话说到一半人就没了；
  #  · 判断里别写 `|| continue`：它在函数体里跳过的是这一轮循环，但读代码的人
  #    （包括三天前的我）会以为顺手把后面的活儿也跳过了。层层 if 更不容易读错。
  f=""; cdir=""; still_there=""; bindir=""
  # 命令里记着「我属于哪个目录」—— 用它判断该不该删。
  # 这份解析不支持行首缩进：生成的命令里 DIR 是第一列，这里按第一列取。
  cli_dir() { # file
    sed -n 's/^DIR="\(.*\)"$/\1/p' "$1" 2>/dev/null | head -1
  }
  for f in /usr/local/bin/simpleurl "$HOME/.local/bin/simpleurl"; do
    # 这里刻意用 if 而不是「没文件就 continue」：注释里那条老写法最容易被
    # 顺手抄回去，而它正是「第二个路径永远不检查」这起事故的起点。
    if [ -f "$f" ]; then
      cdir="$(cli_dir "$f")"
      if [ "$cdir" = "$INSTALL_DIR" ]; then
        { $SUDO rm -f "$f" || true; }
      elif [ -n "$cdir" ] && [ "$cdir" != "$INSTALL_DIR" ] && [ -f "$cdir/simpleurl" ]; then
        still_there="$cdir"
      fi
    fi
  done
  # 全局命令这条线索断了：再找两条。
  #  1. 安装记录里记着的兄弟目录（.siblings，每次安装都会把「机器上还有谁」写进去）
  #  2. 从旧命令里抄下来的目录（.last-cli），最后一条退路
  # 为什么不扫 $HOME：`for d in "$HOME"/*/` 在没有匹配时会把字面量 "/root/*" 当成
  # 一个目录名，后面的判断全部落空却又不报错 —— 看着「扫过了」，其实什么也没扫到；
  # 而把搜索范围铺到整个 $HOME 去找一个标记文件，代价和收益都不对等。
  #  · 用 if 而不是 continue：continue 在函数体里跳过的是「整个函数剩下的部分」那种
  #    误读太容易发生，改成层层 if，读起来就是它实际执行的样子
  for cdir in $(sed -n 's/^dir=//p' "$SIBLING_FILE" 2>/dev/null) \
                $(sed -n 's/^last_dir=//p' "$MARK_FILE" 2>/dev/null); do
    if [ -n "$cdir" ] && [ "$cdir" != "$INSTALL_DIR" ] \
       && [ -f "$cdir/.simpleurl-managed" ] && [ -x "$cdir/simpleurl" ]; then
      still_there="$cdir"
      break
    fi
  done

  # 卸载是唯一「删整个目录」的地方：路径异常（空 / / / $HOME）时宁可不删
  case "$INSTALL_DIR" in
    ""|"/"|"$HOME"|"$HOME/"|"/root"|"/usr"|"/usr/local") die "安装目录 ${INSTALL_DIR} 不像本脚本装的，拒绝删除" ;;
  esac
  rm -rf "${INSTALL_DIR:?}"
  # 本目录的全局命令删掉了，隔壁那份还在：把命令接回去，别让人以为装的东西没了。
  if [ -n "$still_there" ] && [ "$still_there" != "$INSTALL_DIR" ] && [ -f "$still_there/simpleurl" ]; then
    # 落点跟着权限走：之前一律往 /usr/local/bin 写，非 root 时 cp 失败，
    # 屏幕上什么也没说，结果就是「隔壁那份明明还在，命令却没了」
    bindir="/usr/local/bin"
    [ "$(id -u)" = "0" ] || bindir="$HOME/.local/bin"
    $SUDO mkdir -p "$bindir" 2>/dev/null || mkdir -p "$bindir" 2>/dev/null || true
    if $SUDO cp -f "$still_there/simpleurl" "$bindir/simpleurl" 2>/dev/null; then
      ok "全局命令改指向仍在用的那份：$still_there"
    else
      warn "没能把命令接回去（权限？）。隔壁那份可以直接用：$still_there/simpleurl"
    fi
  fi
  ok "已卸载干净"
}

# ---------------------------------------------------------------- 子命令转发
# 帮助里列出的管理子命令由安装时生成的命令负责实现，脚本这里只做转发。
# 不重复实现一遍：两份逻辑迟早会漂移，而漂移的是「文档说的」和「实际做的」。
forward_subcommand() {
  local sub="$1" cli=""
  shift
  if [ ! -f "$MARK_FILE" ]; then
    echo "还没装好（$INSTALL_DIR 里没有安装记录）。先跑：./install.sh --yes" >&2
    return 1
  fi
  # 只认「这套安装自己的」命令。以前兜底直接拿 $PATH 里的 simpleurl，
  # 机器上装着两份 SimpleURL 时，`./install.sh status --dir /a` 会转到
  # /usr/local/bin/simpleurl（指向 /b）上去——症状是「明明指定了目录，
  # 打印/改动的却是另一个站」。现在 PATH 里那条也必须先核对 DIR 再采用。
  local cand cdir foreign=""
  for cand in "$INSTALL_DIR/simpleurl" \
              "$(command -v simpleurl 2>/dev/null || true)" \
              /usr/local/bin/simpleurl "$HOME/.local/bin/simpleurl"; do
    [ -n "$cand" ] && [ -x "$cand" ] || continue
    cdir="$(sed -n 's/^DIR="\(.*\)"$/\1/p' "$cand" 2>/dev/null | head -1)"
    if [ "$cand" = "$INSTALL_DIR/simpleurl" ] || [ -z "$cdir" ] || [ "$cdir" = "$INSTALL_DIR" ]; then
      cli="$cand"; break
    fi
    [ -n "$foreign" ] || foreign="$cdir"
  done
  # 目录里的影子命令不在（老安装 / 手动删过）：用安装目录里的脚本自己顶上。
  # `simpleurl uninstall` 的第 303 行就是 exec 到 install.sh，找不到它等于
  # 命令里最需要能用的那条永远用不了。
  if [ -z "$cli" ] && [ -x "$INSTALL_DIR/install.sh" ]; then
    exec bash "$INSTALL_DIR/install.sh" "$sub" ${1+"$@"}
  fi
  # 机器上只装了别的目录那一份：不要静悄悄地转过去，也不要装作没装。
  # 说清楚谁负责这个目录，把下一步给人。
  if [ -z "$cli" ] && [ -n "$foreign" ]; then
    cat >&2 <<MSG
$(printf '%s!%s ' "$YELLOW" "$RST")这个目录（$INSTALL_DIR）没有自己的 simpleurl 命令
    机器上的 simpleurl 指向：$foreign
    重跑一次生成：SIMPLEURL_HOME=$INSTALL_DIR bash $INSTALL_DIR/install.sh --yes
MSG
    return 1
  fi
  if [ -z "$cli" ]; then
    echo "找不到 simpleurl 命令。重跑一次 ./install.sh --yes 即可生成" >&2
    return 1
  fi
  exec "$cli" "$sub" ${1+"$@"}
}

# ---------------------------------------------------------------- 主流程
# 读已有安装的配置：升级时原样沿用，不重新提问
load_existing_env() { # envfile
  local f="$1" raw_token
  SITE_NAME="$(grep -E '^SITE_NAME=' "$f" | head -1 | cut -d= -f2- || echo "$SITE_NAME")"
  PORT="$(grep -E '^PORT=' "$f" | head -1 | cut -d= -f2- || echo "$PORT")"
  PUBLIC_URL="$(grep -E '^PUBLIC_URL=' "$f" | head -1 | cut -d= -f2- || echo "")"
  ACCESS_MODE="$(grep -E '^ACCESS_MODE=' "$f" | head -1 | cut -d= -f2- || echo "open")"
  SECRET_KEY="$(grep -E '^SECRET_KEY=' "$f" | head -1 | cut -d= -f2- || echo "")"
  # 凭据先看账号密码两行；老安装只写了 ADMIN_TOKEN，按同样的约定读回来
  ADMIN_USER="$(grep -E '^ADMIN_USERNAME=' "$f" | head -1 | cut -d= -f2- || echo "")"
  ADMIN_PASS="$(grep -E '^ADMIN_PASSWORD=' "$f" | head -1 | cut -d= -f2- || echo "")"
  raw_token="$(grep -E '^ADMIN_TOKEN=' "$f" | head -1 | cut -d= -f2- || echo "")"
  if [ -z "$ADMIN_PASS" ]; then
    case "$raw_token" in
      *:*) ADMIN_USER="${ADMIN_USER:-${raw_token%%:*}}"; ADMIN_PASS="${raw_token#*:}" ;;
      "")  : ;;
      *)   ADMIN_PASS="$raw_token" ;;
    esac
  fi
  ADMIN_USER="${ADMIN_USER:-admin}"
}

do_install() {
  # banner 只打一次：main 里已经打过，别再重复刷屏
  [ "$BANNER_SHOWN" = "y" ] || { banner; BANNER_SHOWN="y"; }

  # 已有安装：沿用配置，别把人已经配好的东西再问一遍
  local existing_env="$INSTALL_DIR/.env"
  local reuse="n"
  local prev_runtime=""
  [ -f "$MARK_FILE" ] && prev_runtime="$(sed -n 's/^runtime=//p' "$MARK_FILE" | head -1)"
  # 端口按安装记录走。`./install.sh --yes` 在已有安装上是「重装同一份代码」，
  # 它以前却不读记录：端口还是默认的 8940，体检于是拿 8940 去查，
  # 报一句「8940 空闲」，紧接着在 18950 上重启服务。现在缺省即沿用记录，
  # 只有显式 --port / PORT= 才覆盖。
  if [ -z "$PORT_EXPLICIT" ] && [ -f "$MARK_FILE" ]; then
    local rec_port; rec_port="$(sed -n 's/^port=//p' "$MARK_FILE" | head -1)"
    [ -n "$rec_port" ] && PORT="$rec_port"
  fi

  if [ -f "$existing_env" ]; then
    reuse="y"
    step "检测到已有安装：$INSTALL_DIR（沿用现有配置，只更新代码）"
    load_existing_env "$existing_env"
    [ -n "$RUNTIME_OVERRIDE" ] && prev_runtime="$RUNTIME_OVERRIDE"
  fi

  probe_network
  if [ "$CN_MIRROR_ON" = "1" ]; then
    ok "网络探测：国内线路，启用加速（pip${PIP_INDEX_URL:+ · $PIP_INDEX_URL}）"
  else
    ok "网络探测：直连官方源"
  fi

  # 换壳重跑的那一轮不再体检：A 轮刚跑过、结论就是「可以装」，重跑一遍只会在屏幕上
  # 多出一份结论，还可能因为「端口正被自己占着」前后说法不一。
  if [ "${SCRIPT_DOCTORED:-n}" = "y" ]; then
    ok "环境体检已在换脚本前通过"
  else
    doctor || die "环境体检没过，按上面的提示处理后再跑一次"
  fi

  # 记下「换代码之前」的版本，装完好说清这轮到底动了什么
  PREV_CODE_VERSION=""
  [ -f "$MARK_FILE" ] && PREV_CODE_VERSION="$(sed -n 's/^code_version=//p' "$MARK_FILE" | head -1)"

  # 升级前先看一眼远端版本：已经最新就不折腾了，别每次都重装依赖、重启服务。
  # 注意命令顺序：函数调用放最后，「函数返回 1」就不会把整个 if 判成失败。
  # 换脚本要对**升级和重装都做**：`./install.sh` 在已有安装上就是「重装代码」，
  # 以前只有 upgrade 换壳，于是从仓库目录重跑一次就把安装目录里的脚本打回旧版
  # ——机器上从此跑着旧脚本，新脚本里的修法一个都用不上。
  if [ "$reuse" = "y" ] && [ "$SCRIPT_REEXEC" != "y" ] && [ "$SCRIPT_FETCHED" != "y" ]; then
    local fresh_script REEXEC_ACTION; fresh_script="$(fetch_latest_script)"
    REEXEC_ACTION="install"
    [ "$ACTION_UPGRADE" = "y" ] && REEXEC_ACTION="upgrade"
    if [ -n "$fresh_script" ]; then
      SCRIPT_URL_HINT="$fresh_script"
      # 只有远端「更新」才换壳。用 != 而不是「内容不同就换」：
      # 装的是刚改过的本地版本时，远端 main 反而是旧的 —— 那种情况下换壳等于
      # 把本地这份原地降级（跑一遍升级，最后落盘的脚本比开始前还老）。
      if version_lt "$(script_version "$INSTALL_DIR/install.sh")" "$(script_version "$fresh_script")"; then
        ok "安装脚本可更新（v$(script_version "$INSTALL_DIR/install.sh") → v$(script_version "$fresh_script")），换成远端最新版再继续"
        # 换壳重跑：本进程还带着旧脚本已经解析好的逻辑，直接往下跑等于没换
        SCRIPT_REEXEC="y"
        # 换壳重跑要带着这几件事过去，否则屏幕上同一件事会报两遍、还可能报成两种：
        #  · RUNTIME_OVERRIDE：A 轮认出的运行方式。不带过去，B 轮会把体检结论重算一遍。
        #  · SCRIPT_DOCTORED=y：体检刚跑过、结论是「可以装」，B 轮不用再跑。
        #  · SIMPLEURL_PORT_FIXED=y：A 轮已经就端口做过决定，B 轮别再「自动换个口」。
        #  · PORT / PORT_EXPLICIT：端口是从配置文件读回来的值，不是用户显式指定的 ——
        #    不带过去，B 轮会当它是默认值重新体检，发现被自己占着又「自动换口」，
        #    于是「重装」把服务搬到了隔壁端口。显式指定也同理：B 轮拿不到证据，
        #    会把用户要的端口当成冲突处理掉。
        # 其余显式参数（--no-health / --force / --strict / --branch）一并带过去：
        # 之前一个都没传，A 轮按 --no-health 跳过验收，B 轮照样等 60 秒再报失败。
        _reexec_args=("$REEXEC_ACTION" --yes)
        [ "$SKIP_HEALTH" = "y" ] && _reexec_args+=(--no-health)
        [ "$ASSUME_YES" = "y" ] && _reexec_args+=(--force)
        [ "$STRICT" = "y" ] && _reexec_args+=(--strict)
        [ "$PORT_EXPLICIT" = "y" ] && _reexec_args+=(--port "$PORT")
        [ "$REPO_BRANCH" != "main" ] && _reexec_args+=(--branch "$REPO_BRANCH")
        SIMPLEURL_HOME="$INSTALL_DIR" SCRIPT_REEXEC=y \
          RUNTIME_OVERRIDE="${RUNTIME_OVERRIDE:-$prev_runtime}" \
          SCRIPT_DOCTORED=y SIMPLEURL_PORT_FIXED=y \
          PORT="$PORT" PORT_EXPLICIT="$PORT_EXPLICIT" \
          exec bash "$fresh_script" ${_reexec_args[@]+"${_reexec_args[@]}"}
      fi
      rm -rf "$INSTALL_DIR/.stage-script"
    else
      warn "取不到远端安装脚本，本轮沿用本地这份（升级代码不受影响）"
    fi
    # 一次尝试就够：短路升级会在下面 exit 0，重跑时又白拉一遍
    SCRIPT_FETCHED="y"
  fi

  if [ "$ACTION_UPGRADE" = "y" ] && [ "$reuse" = "y" ]; then
    local same_rc=0
    same_as_remote || same_rc=$?
    if [ "$same_rc" = "0" ]; then
      # 说清「最新」是怎么判的：只比 app/__init__.py 里的代码版本，不比提交。
      # 代码没动、只改了安装脚本或文档时，这里也会说「已经是最新」—— 别让人以为升级假跑了。
      ok "代码已是最新（v$REMOTE_PEEK，与本机记录一致），服务与依赖不动"
      [ -n "$(local_code_version)" ] && [ "$(script_version "$INSTALL_DIR/install.sh")" != "$VERSION" ] \
        && warn "安装目录里的脚本是 v$(script_version "$INSTALL_DIR/install.sh")，这份是 v$VERSION（重装即会同步）"
      local url="http://$(detect_ip):$PORT"
      [ -n "$PUBLIC_URL" ] && url="$PUBLIC_URL"
      printf "  站点 %s\n  后台 %s/admin\n\n" "$url" "$url"
      exit 0
    fi
    # 探不到远端（离线 / 没令牌 / 网络不通）不能当成「已最新」：那会让升级永远空转。
    # 也不能当成「有新版」：离线机器每次都会白下载一遍。说清楚，然后照常走一遍。
    [ "$same_rc" = "2" ] && warn "探不到远端版本（没令牌或网络受限），照常下载确认"
  fi

  # 旧进程要在换代码之前停：它占着端口、拿着 SQLite 的写锁，还在 import 即将被覆盖的
  # 文件。等装完再停，就会出现「服务其实一直在跑旧代码」这种最难查的状态。
  if [ "$reuse" = "y" ]; then
    stop_running
  elif port_in_use "$PORT"; then
    # 首次安装但端口不干净：check_port_free 已经拦下来问过了（显式端口会照办），
    # 这里负责真把本目录的孤儿清掉。以前这一步只在「重装」时做，于是
    # 「删掉安装目录再装回去」会一路顺利到启动才 bind 失败，错误信息指向端口，
    # 而真正的原因是上一轮留下的进程一直没人收。
    stop_running
  fi

  if [ "$reuse" = "n" ]; then
    echo
    hr; printf "%s装之前先确认几件事（回车用默认值）%s\n" "$BOLD" "$RST"; hr
    ask "站点名称" "$SITE_NAME"; SITE_NAME="$ANSWER"
    ask "监听端口" "$PORT"; PORT="$ANSWER"
    check_port_free

    ask "站点域名（如 s.example.com，没有就留空用 IP）" ""
    PUBLIC_URL=""
    if [ -n "$ANSWER" ]; then
      case "$ANSWER" in
        http://*|https://*) PUBLIC_URL="${ANSWER%/}" ;;
        *) PUBLIC_URL="http://$ANSWER" ;;
      esac
      case "$PUBLIC_URL" in
        *":$PORT"|https://*) : ;;
        *) PUBLIC_URL="$PUBLIC_URL:$PORT" ;;
      esac
      ok "对外地址：$PUBLIC_URL"
      # 有域名就该配 HTTPS：顺手生成一份能用的反代配置，别让人自己拼
      local domain_only="${PUBLIC_URL#*://}"; domain_only="${domain_only%%/*}"; domain_only="${domain_only%%:*}"
      if proxy_available; then
        step "生成反向代理配置（$domain_only）"
        write_proxy_config "$domain_only" || warn "没生成反代配置，可手动按 README 的 Nginx 段落配"
      else
        warn "没检测到 nginx / caddy / apache；装了以后重跑本脚本会补上反代配置"
      fi
    else
      warn "未填域名，装完用 http://<服务器IP>:$PORT 访问"
    fi

    ask "管理员账号" "admin"; ADMIN_USER="${ANSWER:-admin}"
    ask_password "管理员密码"
    ADMIN_PASS="${ANSWER:-$(gen_password)}"

    # 选项要和站点真正认的值一致。站点只认 open / admin（见 app/core/access.py），
    # 以前这里问的是 public / admin / invite：public 落到 normalize_mode 里会被
    # 兜底成 open，「刚好」是对的；而 invite（邀请码）那个模式在早几轮重构里
    # 已经没有了，选它等于什么都没发生。写对一次，比事后解释省事。
    ask "访问模式 open 谁都能建 / admin 仅管理员" "open"; ACCESS_MODE="$ANSWER"
    case "$ACCESS_MODE" in open|admin) : ;; *) ACCESS_MODE="open" ;; esac
    SECRET_KEY="$(gen_key 32)"
  else
    # 升级时绝不能悄悄换掉凭据：
    #  · ADMIN_TOKEN 读不到 → 生成新密码，屏幕上滚过去的那一行就是唯一线索；
    #    更糟的是 summary 会把新密码当成「当前密码」打出来，而库里/index 里
    #    早先抄走的那份就此失效。这里只提醒，不动它。
    #  · SECRET_KEY 换掉 = 所有已登录的会话当场下线、签名回执全部作废。
    # 这两样都只在「首次安装」时才允许生成。
    [ -n "${ADMIN_PASS:-}" ] || warn "安装记录里读不到管理员密码，沿用原配置不动（要换密码：simpleurl password --reset）"
    [ -n "${SECRET_KEY:-}" ] || {
      SECRET_KEY="$(gen_key 32)"
      warn "读不到 SECRET_KEY，生成了新的：已登录会话会全部失效"
    }
    # 老安装里存的 public 等价于 open，先归一，后面写 .env / 日报都用得着
    case "${ACCESS_MODE:-}" in open|admin) : ;; public|"") ACCESS_MODE="open" ;; *) ACCESS_MODE="open" ;; esac
  fi

  echo
  install_source

  # 运行方式：显式参数 > 上次安装的方式 > 有 Docker 用 Docker > Python
  local want="$RUNTIME_OVERRIDE"
  [ -n "$want" ] || want="$prev_runtime"
  if [ -z "$want" ]; then
    if have_docker; then want="docker"; elif have_python; then want="python"; else want=""; fi
  fi
  [ -n "$want" ] || die "既没有 Docker 也没有 Python 3.10+，先装一个再来"
  case "$want" in
    docker) have_docker || { warn "指定了 Docker 方式，但 Docker 不可用，改用 Python"; want="python"; } ;;
    python) : ;;
  esac

  if [ "$want" = "docker" ]; then
    install_docker_mode
  else
    if [ "$reuse" = "y" ] && [ "$prev_runtime" = "docker" ]; then
      warn "上次是 Docker 装的，这次改用 Python：数据留在数据卷里，需手动搬迁"
      docker rm -f "$SERVICE_NAME" >/dev/null 2>&1 || true
    fi
    warn "使用 Python 直装（更省资源）"
    install_python_mode
    DATABASE_URL="sqlite:///$INSTALL_DIR/simpleurl.db"
    # 升级时不要把 .env 整份重写：README 教用户在 .env 里设 PUBLIC_URL、改端口，
    # 而重写会把人家手写的那几行冲掉。只在「首次安装」或「文件不在」时生成。
    if [ "$reuse" = "n" ] || [ ! -f "$existing_env" ]; then
      write_env "$existing_env"
    else
      ok "沿用现有配置：$existing_env（要改就 simpleurl config）"
    fi
    write_start_wrapper
    if ! setup_systemd; then
      if ! start_background; then
        # systemd 环境里注册失败了但进程可能还在，先清干净再回滚
        stop_running
        rollback_or_die "服务没能启动"
      fi
    fi
  fi

  # 装进去的东西真的对得上吗？这一步抓的是「装了个寂寞」：脚本靠标记文件里的
  # code_version 判断「已经最新」，而那个值是从标记文件读回来的 —— 只要复用了旧
  # 标记，屏幕上就会说「代码未变动」，即便落到目录里的其实是另一份代码。
  # 所以不看记录，直接问装进去那份代码自己：能不能 import、版本是多少。
  check_installed_code || die "$INSTALL_DIR/app 跑不起来（依赖装好了但 import 失败，看 simpleurl log）"

  CODE_VERSION="$(remote_version "$INSTALL_DIR")"
  # last_dir：装的时候全局命令指向哪儿。卸载时它被覆盖过（指向别处）也用得上。
  _last_dir=""
  for _cmd in /usr/local/bin/simpleurl "$HOME/.local/bin/simpleurl"; do
    [ -f "$_cmd" ] || continue
    _d="$(sed -n 's/^DIR="\(.*\)"$/\1/p' "$_cmd" 2>/dev/null | head -1)"
    [ -n "$_d" ] && [ "$_d" != "$INSTALL_DIR" ] && { _last_dir="$_d"; break; }
  done
  printf 'version=%s\ncode_version=%s\nruntime=%s\nport=%s\ninstalled_at=%s\nlast_dir=%s\n' \
    "$VERSION" "$CODE_VERSION" "$RUNTIME" "$PORT" \
    "$(date '+%Y-%m-%d %H:%M:%S')" "$_last_dir" > "$MARK_FILE"
  install_cli
  # 走到这里代码已换完、服务已起来、验收也过了 —— 只有这时才换脚本自己。
  # 换早了没意义：中途任何一步失败，留下的该是「还能跑旧那一版」的现场。
  self_update_script
  if [ "$SKIP_HEALTH" = "y" ]; then
    warn "按参数跳过健康检查"
  else
    wait_health 60 || die "服务起来了但健康检查没过，看 simpleurl log 定位（数据已备份，可 simpleurl restore 回滚）"
    # 健康检查过了不等于能用：口令写坏、接口被拦都还是 200 的壳
    if ! verify_end_to_end; then
      warn "服务在跑，但业务自检没过 —— 这不算装好。下面是定位入口："
      warn "  simpleurl log        看服务日志"
      warn "  simpleurl doctor     重新体检环境"
      warn "  数据未受影响，可 simpleurl restore <备份> 回滚"
      printf "\n%s服务已启动，但验收未通过（站点仍可访问：http://127.0.0.1:%s）%s\n\n" \
        "$YELLOW" "$PORT" "$RST" >&2
      exit 1
    fi
  fi
  summary
  return 0
}

main() {
  case "$ACTION" in
    version) printf 'SimpleURL 安装脚本 v%s\n' "$VERSION"; exit 0 ;;
    status|start|stop|restart|log|url|password|config|backup|restore)
      forward_subcommand "$ACTION" ${SUB_ARGS[@]+"${SUB_ARGS[@]}"} ;;
    help) usage; exit 0 ;;
    doctor) banner; doctor; exit $? ;;
    uninstall) banner; do_uninstall; exit 0 ;;
    upgrade)
      ACTION="install"; ACTION_UPGRADE="y"
      [ -f "$INSTALL_DIR/.env" ] || die "$INSTALL_DIR 里没有安装记录，用 install 而不是 upgrade"
      # banner 由 do_install 自己打（只打一次），这里再打一遍就是两遍
      do_install; exit 0 ;;
    install)
      do_install ;;
  esac
}

main "$@"
