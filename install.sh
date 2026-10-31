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

VERSION="2.3.0"
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
  case "$GIT_HOST" in
    *cnb.cool*)
      # 有令牌走 OpenAPI（私库也能拉、拿得到真 tar.gz）；没令牌退回网页路由，
      # 由 is_tarball 挡掉 HTML 外壳后自动改用 git clone
      if [ -n "${CNB_TOKEN:-}" ]; then
        printf '%s/%s/-/git/archive/%s.tar.gz' "$API_HOST" "$REPO_SLUG" "$REPO_BRANCH"
      else
        printf '%s/%s/-/archive/%s.tar.gz' "$GIT_HOST" "$REPO_SLUG" "$REPO_BRANCH"
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

# LOG_FILE / MARK_FILE 都是从 INSTALL_DIR 派生的常量，而 --dir 在参数解析时才读到。
# 早先只改 INSTALL_DIR、没重算这两个，结果拿到 --dir 的安装把日志往
# $HOME/simpleurl 写 —— 目录不存在，nohup 当场失败，报一句跟真正原因无关的错。
CLI_DIR="$(dir_from_args "$@")"
SIMPLEURL_HOME="${SIMPLEURL_HOME:-$CLI_DIR}"
if [ -n "${SIMPLEURL_HOME:-}" ]; then
  INSTALL_DIR="$SIMPLEURL_HOME"
elif [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/.simpleurl-managed" ]; then
  INSTALL_DIR="$SELF_DIR"
else
  # 第三条路：本机已经装过（CLI 里记着目录）。从任意位置跑 `bash install.sh upgrade`
  # 是最自然的用法，不该因为「脚本不在安装目录里」就跑去 $HOME/simpleurl 报
  # 「没有安装记录」。装了多份时以最近一次安装为准。
  INSTALL_DIR="$HOME/simpleurl"
  for _mark in /usr/local/bin/simpleurl "$HOME/.local/bin/simpleurl"; do
    [ -f "$_mark" ] || continue
    _dir="$(sed -n 's/^DIR="\(.*\)"$/\1/p' "$_mark" 2>/dev/null | head -1)"
    if [ -n "$_dir" ] && [ -f "$_dir/.simpleurl-managed" ]; then INSTALL_DIR="$_dir"; break; fi
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
ASSUME_YES="n"
SKIP_HEALTH="n"

while [ $# -gt 0 ]; do
  case "$1" in
    install)     ACTION="install" ;;
    upgrade|update) ACTION="upgrade" ;;
    --yes|-y)    INTERACTIVE=0 ;;
    uninstall|remove) ACTION="uninstall"; INTERACTIVE=0 ;;
    doctor|check) ACTION="doctor" ;;
    --runtime)   shift; RUNTIME_OVERRIDE="${1:-}" ;;
    --branch)    shift; REPO_BRANCH="${1:-main}"; REPO_TARBALL="$(default_tarball)" ;;
    --port)      shift; PORT="${1:-$PORT}" ;;
    # --dir 要同时改写 SIMPLEURL_HOME：install_cli / 卸载 / 升级都从它算安装目录，
    # 只改 INSTALL_DIR 会让后面几步偷偷回到 $HOME/simpleurl
    --dir)       shift; SIMPLEURL_HOME="${1:-$SIMPLEURL_HOME}"; INSTALL_DIR="$SIMPLEURL_HOME"
                 LOG_FILE="$INSTALL_DIR/simpleurl.log"; MARK_FILE="$INSTALL_DIR/.simpleurl-managed" ;;
    --no-health) SKIP_HEALTH="y" ;;
    --force)     ASSUME_YES="y" ;;
    --help|-h)   ACTION="help" ;;
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
  ./install.sh --help         这段说明

  可选参数：--port 8000  --dir ~/simpleurl  --runtime docker|python  --no-health

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

is_cnb() { case "$GIT_HOST" in *cnb.cool*) return 0 ;; esac; return 1; }

# 私有仓库：CNB_TOKEN（或环境里的 CNB 令牌）会被换成 Basic 头，可直接拉私库源码
auth_header() {
  local tok="${CNB_TOKEN:-}"
  [ -n "$tok" ] || return 0
  printf 'Authorization: Basic %s' "$(printf 'cnb:%s' "$tok" | base64 | tr -d '\n')"
}

fetch() { # url out
  local auth; auth="$(auth_header)"
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
    [ -n "${CNB_TOKEN:-}" ] && urls+=("$API_HOST/$REPO_SLUG/-/git/raw/$REPO_BRANCH/app/__init__.py")
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

port_in_use() { # port
  if need_cmd ss; then ss -lnt 2>/dev/null | awk -v p=":$1" '$4 ~ p"$" {found=1} END{exit !found}'
  elif need_cmd lsof; then lsof -ti "tcp:$1" >/dev/null 2>&1
  elif need_cmd netstat; then netstat -lnt 2>/dev/null | awk -v p=":$1" '$4 ~ p"$" {found=1} END{exit !found}'
  else return 1; fi
}

# 端口被谁占着、占着它的是不是「自己人」。命中返回 0 并把 pid 写进 PORT_HOLDER_PID。
# 用 ss 拿 pid 要 root；拿不到就没法判断，按「不是自己人」处理 —— 宁可不杀，
# 也不要因为认错而误杀别人的服务。
PORT_HOLDER_PID=""
port_holder_pid() { # port
  PORT_HOLDER_PID=""
  need_cmd ss || return 1
  local line
  line="$(ss -lntp 2>/dev/null | awk -v p=":$1" '$4 ~ p"$" {print; exit}')"
  [ -n "$line" ] || return 1
  PORT_HOLDER_PID="$(printf '%s' "$line" | sed -n 's/.*pid=\([0-9]*\).*/\1/p')"
  [ -n "$PORT_HOLDER_PID" ]
}

# 这个 pid 是上次本脚本留下的服务进程吗？两条件：它自己的命令行落在安装目录里，
# 或者它就是标记文件里记下的那个 pid。除此之外一律不动。
is_our_service() { # pid
  local pid="$1" cwd=""
  [ -n "$pid" ] || return 1
  # 没有 /proc（macOS、部分精简容器）或当前用户看不到别的用户的进程时，
  # 这里一律判定「不是自己人」：宁可多报一次端口占用（并换个口），
  # 也不能因为读不到就默认「自己的」然后把陌生进程杀掉。
  [ -d "/proc/$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  if [ -f "$INSTALL_DIR/simpleurl.pid" ] && [ "$(cat "$INSTALL_DIR/simpleurl.pid" 2>/dev/null)" = "$pid" ]; then
    return 0
  fi
  cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null || true)"
  [ "$cwd" = "$INSTALL_DIR" ] && return 0
  tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q -F -- "$INSTALL_DIR" && return 0
  return 1
}

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
  # 端口。重装 / 升级时被上次的自己占着是常态，报冲突只会吓人一跳
  if ! port_in_use "$PORT"; then
    d_ok "端口 $PORT 空闲"
  elif port_holder_pid "$PORT" && is_our_service "$PORT_HOLDER_PID"; then
    d_ok "端口 $PORT 由上次安装的 SimpleURL 自己占着（重装会先停掉）"
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

  # 对外地址
  local ip; ip="$(detect_ip)"
  is_private_ip "$ip" && d_ok "本机地址 $ip" || d_ok "本机地址 $ip（公网）"

  printf "\n"
  if [ "$DOCTOR_FAIL" -gt 0 ]; then
    printf "%s%d 项阻塞，%d 项提醒%s\n" "$RED" "$DOCTOR_FAIL" "$DOCTOR_WARN" "$RST"
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
SOURCE_ENTRIES="app requirements.txt requirements-dev.txt .env.example Dockerfile docker-compose.yml
.dockerignore .gitignore pytest.ini run.sh scripts README.md .cnb.yml"
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

stage_source() { # 把最新代码放到 $1（一个空目录）。失败返回非 0
  local stage="$1" urls=() u tarball
  tarball="$stage/.src.tar.gz"

  # 目录里的脚本给自己跑（安装目录里的 install.sh），不用重复下载；
  # 但 /workspace 拷出去的脚本没有 install.sh，得从远端取 —— 否则装出来是半截。
  # `upgrade` 例外：升级的全部意义就是「拿远端那份」，拿本地现成的等于原地转圈，
  # 还会报一句「源码已就绪」骗人。
  if [ "$ACTION_UPGRADE" != "y" ] && [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/app/main.py" ] \
     && [ "$SELF_DIR" = "$(cd "$INSTALL_DIR" 2>/dev/null && pwd || echo "")" ] \
     && [ -f "$SELF_DIR/install.sh" ]; then
    return 2
  fi

  # 源码包优先：不依赖 git，也不受仓库体积影响
  [ "$(mirror_url "$REPO_TARBALL")" != "$REPO_TARBALL" ] && urls+=("$(mirror_url "$REPO_TARBALL")")
  urls+=("$REPO_TARBALL")
  for u in ${urls[@]+"${urls[@]}"}; do
    rm -f "$tarball"
    # is_tarball 认 gzip 魔数，网页外壳（HTML）在这一步就被挡掉
    if fetch "$u" "$tarball" >/dev/null 2>&1 && is_tarball "$tarball"; then
      if tar -xzf "$tarball" -C "$stage" --strip-components=1 2>/dev/null && [ -f "$stage/app/main.py" ]; then
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
apply_source() { # stage
  local stage="$1" stash="" f
  stash="$(stash_user_files "$stage/.userfiles")"
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
    ok "本地版本与安装目录一致，跳过下载"
    SOURCE_REUSED="y"
    return 0
  fi
  if [ "$rc" != "0" ]; then
    rm -rf "$stage"
    # 已有安装 / 离线环境：「下载失败」不等于不能继续
    if [ -f "$INSTALL_DIR/app/main.py" ]; then
      warn "拿不到远端源码，沿用 $INSTALL_DIR 里已有的代码继续"
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
    printf 'ADMIN_TOKEN=%s:%s\n' "$ADMIN_USER" "$ADMIN_PASS"
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
    -e "ADMIN_TOKEN=$ADMIN_USER:$ADMIN_PASS" \
    -e "PUBLIC_URL=$PUBLIC_URL" -e "ACCESS_MODE=$ACCESS_MODE" \
    -e "TZ=${TZ:-Asia/Shanghai}" \
    --label "simpleurl.managed=1" \
    simpleurl:latest >/dev/null || rollback_or_die "容器启动失败"
  RUNTIME="docker"
  ok "容器已启动：$SERVICE_NAME（数据卷 ${SERVICE_NAME}-data）"

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
# 后台凭据：admin:密码 表示账号密码分开设；只写口令则账号固定 admin
ADMIN_TOKEN=$ADMIN_USER:$ADMIN_PASS
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

stop_running() {
  if [ "$RUNTIME" = "docker" ]; then
    docker stop "$SERVICE_NAME" >/dev/null 2>&1 || true
  elif has_systemd_unit && need_cmd systemctl; then
    local SUDO=""; [ "$(id -u)" != "0" ] && need_cmd sudo && SUDO="sudo"
    $SUDO systemctl stop "$SERVICE_NAME" >/dev/null 2>&1 || true
  elif [ -f "$INSTALL_DIR/simpleurl.pid" ]; then
    local pid; pid="$(cat "$INSTALL_DIR/simpleurl.pid" 2>/dev/null || echo "")"
    # pid 文件可能过期：机器重启过、进程崩过，那个 pid 早被别人复用了。
    # 不核对就 kill，等于把别人的进程顺手杀掉——所以先确认它真在建
    # 目录里跑。核对不上就只删文件，不动进程。
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && is_our_service "$pid"; then
      kill "$pid" 2>/dev/null || true
    fi
    rm -f "$INSTALL_DIR/simpleurl.pid"
  elif port_holder_pid "$PORT" && is_our_service "$PORT_HOLDER_PID"; then
    # 没有 pid 文件（被删过、或是手动起的进程），但端口上确实是自己那个服务：
    # 照样停，否则接下来的启动会撞端口，报一个和真正原因无关的错
    kill "$PORT_HOLDER_PID" 2>/dev/null || true
  fi
  # 等它真的放开端口：紧接着就要覆盖源码了，进程还在读旧文件就是在给自己挖坑
  local i=0
  while [ "$i" -lt 10 ]; do
    port_in_use "$PORT" || break
    i=$((i + 1)); sleep 1
  done
}

# 停掉「占了目标端口但不是本脚本管理的」进程前先问一声，不默默动手
next_free_port() { # start —— 往后找 40 个，都没有就返回空
  local p
  for p in $(seq $(( $1 + 1 )) $(( $1 + 40 ))); do
    port_in_use "$p" || { printf '%s' "$p"; return 0; }
  done
  printf ''
}

check_port_free() {
  port_in_use "$PORT" || return 0
  local who=""
  if need_cmd ss; then who="$(ss -lntp 2>/dev/null | awk -v p=":$PORT" '$4 ~ p"$" {print $NF; exit}')"; fi
  # 占着端口的就是上次装的那个进程（重装 / 升级时的常态）：这是自己人，
  # 不算冲突。真正要拦的是「别人的服务」——这时候不能默认硬上，也不能自作主张 kill。
  if port_holder_pid "$PORT" && is_our_service "$PORT_HOLDER_PID"; then
    ok "端口 $PORT 由 SimpleURL 自己在用（重装时会先停掉）"
    return 0
  fi
  warn "端口 $PORT 被占用${who:+：$who}"
  local alt; alt="$(next_free_port "$PORT")"
  # 非交互（CI / 面板 / curl | bash --yes）时问不了人，也别硬撞：自己换个空的
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
        curl -s -m 8 -o /dev/null -b "$anon_jar" -X DELETE -H "Origin: $base" \
          -H "X-CSRF-Token: $anon_csrf" "$base/api/links/$code2" 2>/dev/null || true
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
    [ -n "${CNB_TOKEN:-}" ] && urls+=("$API_HOST/$REPO_SLUG/-/git/raw/$REPO_BRANCH/install.sh")
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
  # heredoc 用 <<EOF（要展开 $INSTALL_DIR 等），代价是正文里的反引号也会被安装时执行：
  # 正文里一句 `simpleurl status` 这样的注释，会在安装时被当成命令替换执行掉，
  # 屏幕上多出几行 "command not found"，注释本身也变形。所以正文里一律不写反引号。
  ${SUDO} tee "$cli" >/dev/null <<EOF
#!/usr/bin/env bash
# SimpleURL 管理命令（install.sh $VERSION 生成）
set -uo pipefail
DIR="$INSTALL_DIR"
NAME="$SERVICE_NAME"
MARK="\$DIR/.simpleurl-managed"
G=\$'\033[32m'; R=\$'\033[31m'; D=\$'\033[2m'; N=\$'\033[0m'

# 运行方式以安装时写的标记为准，命令本身不再带上过期信息
meta() { sed -n "s/^\$1=//p" "\$MARK" 2>/dev/null | head -1; }
RUNTIME="\$(meta runtime)"
PORT="\$(meta port)"
URL_HINT="\$(grep -E '^PUBLIC_URL=' "\$DIR/.env" 2>/dev/null | head -1 | cut -d= -f2-)"
[ -n "\$PORT" ] || PORT=8000

say() { printf '%s\n' "\$*"; }
warn() { printf "%s!%s %s\n" "\$R" "\$N" "\$*"; }

running() {
  if [ "\$RUNTIME" = "docker" ]; then
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "\$NAME"
  elif [ -f "/etc/systemd/system/\$NAME.service" ] && command -v systemctl >/dev/null 2>&1; then
    systemctl is-active --quiet "\$NAME"
  else
    [ -f "\$DIR/simpleurl.pid" ] && kill -0 "\$(cat "\$DIR/simpleurl.pid" 2>/dev/null)" 2>/dev/null
  fi
}

site_url() {
  if [ -n "\$URL_HINT" ]; then printf '%s' "\$URL_HINT"; return; fi
  local ip; ip="\$(hostname -I 2>/dev/null | awk '{print \$1}')"
  printf 'http://%s:%s' "\${ip:-127.0.0.1}" "\$PORT"
}

do_start() {
  if running; then say "已在运行"; return 0; fi
  if [ "\$RUNTIME" = "docker" ]; then
    docker start "\$NAME" >/dev/null && say "已启动容器 \$NAME"; return 0
  elif [ -f "/etc/systemd/system/\$NAME.service" ] && command -v systemctl >/dev/null 2>&1; then
    sudo systemctl start "\$NAME" && say "已启动 systemd 服务 \$NAME"; return 0
  fi
  setsid nohup "\$DIR/start.sh" >>"\$DIR/simpleurl.log" 2>&1 </dev/null &
  echo \$! > "\$DIR/simpleurl.pid"
  say "已后台启动，PID \$(cat "\$DIR/simpleurl.pid")"
}

do_stop() {
  if [ "\$RUNTIME" = "docker" ]; then
    docker stop "\$NAME" >/dev/null 2>&1 && say "已停止容器 \$NAME" || say "容器未运行"
  elif [ -f "/etc/systemd/system/\$NAME.service" ] && command -v systemctl >/dev/null 2>&1; then
    sudo systemctl stop "\$NAME" >/dev/null 2>&1 && say "已停止 \$NAME"
  elif [ -f "\$DIR/simpleurl.pid" ]; then
    kill "\$(cat "\$DIR/simpleurl.pid" 2>/dev/null)" 2>/dev/null && say "已停止"
    rm -f "\$DIR/simpleurl.pid"
  else
    say "未在运行"
  fi
}

# 生成的命令行是 set -uo pipefail 跑的。下面每个 \$ 都必须带反斜杠：
# 这整段在安装时被套进外层 heredoc 展开一次，少了反斜杠就当场展开，
# 到了运行时只剩一个空变量名，报 unbound variable。
wait_port_free() {
  local i=0
  while [ "\$i" -lt 10 ]; do
    if command -v ss >/dev/null 2>&1; then
      ss -lnt 2>/dev/null | awk -v p=":\$PORT" '\$4 ~ p"\$" {found=1} END{exit !found}' || return 0
    elif command -v lsof >/dev/null 2>&1; then
      lsof -ti "tcp:\$PORT" >/dev/null 2>&1 || return 0
    else
      sleep 1; return 0
    fi
    i=\$((i + 1)); sleep 1
  done
  return 0
}

health() {
  command -v curl >/dev/null 2>&1 || return 0
  local code
  code="\$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:\$PORT/healthz" 2>/dev/null || echo 000)"
  if [ "\$code" = "200" ]; then printf "  健康检查 %s200 OK%s\n" "\$G" "\$N"
  else printf "  健康检查 %s%s%s\n" "\$R" "\$code" "\$N"; fi
}

set_password() {
  local pass="\${1:-}" cred
  if [ -z "\$pass" ]; then
    pass="\$(openssl rand -base64 32 2>/dev/null | tr -dc 'A-Za-z2-9' | cut -c1-16)"
  fi
  cred="admin:\$pass"
  if [ "\$RUNTIME" = "docker" ]; then
    docker exec "\$NAME" python - "\$cred" <<'PY'
import pathlib, re, sys
p = pathlib.Path('/data/.env')
text = p.read_text() if p.exists() else ''
text = re.sub(r'^ADMIN_TOKEN=.*\$', '', text, flags=re.M).rstrip('\n')
p.write_text(text + '\nADMIN_TOKEN=' + sys.argv[1] + '\n')
PY
    docker restart "\$NAME" >/dev/null 2>&1 || true
  else
    if sed -i.bak "s|^ADMIN_TOKEN=.*|ADMIN_TOKEN=\$cred|" "\$DIR/.env" 2>/dev/null; then :
    else sed -i '' "s|^ADMIN_TOKEN=.*|ADMIN_TOKEN=\$cred|" "\$DIR/.env"; fi
    rm -f "\$DIR/.env.bak"
    do_stop >/dev/null; sleep 1; do_start >/dev/null
  fi
  say "新密码：\$pass"
  say "账号：admin　后台：\$(site_url)/admin"
  say "（改完记得同步改你自己的密码管理器）"
}

site_status() {
  if running; then printf "%s● 运行中%s\n" "\$G" "\$N"
  else printf "%s● 已停止%s\n" "\$R" "\$N"; fi
  health
  printf "  站点 %s\n  后台 %s/admin\n" "\$(site_url)" "\$(site_url)"
  if [ "\$RUNTIME" = "docker" ]; then docker ps --filter "name=^\$NAME\$" --format '  容器 {{.Status}} · {{.Ports}}' 2>/dev/null || true; fi
  return 0
}

do_backup() {
  local out="\${1:-\$HOME/simpleurl-backup-\$(date +%Y%m%d-%H%M%S).tar.gz}"
  mkdir -p "\$(dirname "\$out")"
  if [ "\$RUNTIME" = "docker" ]; then
    docker run --rm -v "\${NAME}-data:/data" -v "\$(dirname "\$out"):/backup" alpine \\
      tar -czf "/backup/\$(basename "\$out")" -C /data . >/dev/null && say "已备份到 \$out"
  else
    tar -czf "\$out" -C "\$DIR" simpleurl.db .env 2>/dev/null && say "已备份到 \$out"
  fi
}

# 从备份里恢复数据。只覆盖数据库和配置，代码不动。
do_restore() {
  local src="\${1:-}" tmp
  [ -n "\$src" ] && [ -f "\$src" ] || { warn "用法：simpleurl restore <备份文件.tar.gz>"; return 2; }
  if [ "\$RUNTIME" = "docker" ]; then
    warn "容器模式请手动恢复：docker run --rm -v \${NAME}-data:/data -v \$(dirname "\$src"):/b alpine tar -xzf /b/\$(basename "\$src") -C /data"
    return 2
  fi
  tmp="\$(mktemp -d)"
  tar -xzf "\$src" -C "\$tmp" 2>/dev/null || { warn "不是有效的备份包"; rm -rf "\$tmp"; return 2; }
  do_stop >/dev/null
  [ -f "\$tmp/simpleurl.db" ] && cp -a "\$tmp/simpleurl.db" "\$DIR/" && rm -f "\$DIR/simpleurl.db-wal" "\$DIR/simpleurl.db-shm"
  [ -f "\$tmp/.env" ] && cp -a "\$tmp/.env" "\$DIR/"
  rm -rf "\$tmp"
  do_start >/dev/null
  say "已从 \$src 恢复，服务已重启"
}

# 不带参数就报状态：敲下去是想知道「现在怎么样」，不是想读一屏用法。
# 但它必须排在 * 分支之前——case 从上往下匹配，* 在前面就永远轮不到。
case "\${1:-status}" in
  start)   do_start ;;
  stop)    do_stop ;;
  # 停完不能立刻起：uvicorn 放端口要一点时间，抢跑会 bind 失败，
  # 留下一个「restart 之后反而没了」的现场
  restart) do_stop >/dev/null; wait_port_free; do_start ;;
  status)  site_status ;;
  url)     site_url; echo ;;
  log)
    if [ "\$RUNTIME" = "docker" ]; then docker logs --tail 100 -f "\$NAME"
    else tail -n 100 -f "\$DIR/simpleurl.log"; fi ;;
  password) set_password "\${2:-}" ;;
  config)
    if [ "\$RUNTIME" = "docker" ]; then
      docker exec "\$NAME" cat /data/.env 2>/dev/null || cat "\$DIR/.env.docker"
      say ""
      say "（Docker 模式改配置：docker exec -it \$NAME vi /data/.env 后 simpleurl restart）"
    else
      "\${EDITOR:-vi}" "\$DIR/.env"
    fi ;;
  backup)  do_backup "\${2:-}" ;;
  restore) do_restore "\${2:-}" ;;
  upgrade)
    # 升级要连脚本一起换。否则装了 v2.2 的机器永远用 2.2 的脚本去升代码，
    # 新脚本里的修法自己用不上——上一轮的安装目录里躺着的就是旧脚本。
    local script="\$DIR/install.sh"
    if [ -f "\$DIR/install.sh" ]; then
      local newest="\$DIR/install.sh.new"
      if [ -n "\${SU_REMOTE_SCRIPT:-}" ] && [ -s "\$SU_REMOTE_SCRIPT" ]; then
        if bash -n "\$SU_REMOTE_SCRIPT" 2>/dev/null; then cp "\$SU_REMOTE_SCRIPT" "\$newest" 2>/dev/null || true; fi
      fi
      if [ -s "\$newest" ]; then script="\$newest"; fi
    fi
    if [ -f "\$script" ]; then SIMPLEURL_HOME="\$DIR" exec bash "\$script" upgrade --yes
    else warn "缺少 \$DIR/install.sh，请重新执行安装脚本"; exit 2; fi ;;
  doctor)
    if [ -f "\$DIR/install.sh" ]; then exec bash "\$DIR/install.sh" doctor
    else warn "缺少 \$DIR/install.sh，请重新执行安装脚本"; exit 2; fi ;;
  uninstall)
    # 卸载会把 \$DIR 整个删掉，跑在里面的 install.sh 也会一起消失；
    # 先拷一份到临时目录再跑，否则中途读不到脚本后半截
    if [ -f "\$DIR/install.sh" ]; then
      local tmp; tmp="\$(mktemp -d 2>/dev/null || echo "/tmp/.su-uninstall.\$\$")"
      cp "\$DIR/install.sh" "\$tmp/install.sh" && chmod +x "\$tmp/install.sh"
      SIMPLEURL_HOME="\$DIR" exec bash "\$tmp/install.sh" --uninstall
    else warn "缺少 \$DIR/install.sh，请重新执行安装脚本"; exit 2; fi ;;
  "")      site_status ;;
  help|--help|-h|*)
    cat <<'\USAGE' | sed "s/^/  /;s/^  \$//"
SimpleURL 管理命令

simpleurl status              运行状态与健康检查
simpleurl start | stop | restart
simpleurl log                 跟踪日志
simpleurl url                 打印站点与后台地址
simpleurl password [新密码]    重置管理员密码（不给参数就随机生成）
simpleurl config              查看 / 编辑配置
simpleurl backup [路径]        备份数据库与配置
simpleurl restore <备份>       从备份恢复数据
simpleurl upgrade             拉新代码升级（失败自动回滚数据）
simpleurl doctor              环境体检
simpleurl uninstall           卸载
\USAGE
    ;;
esac
EOF
  ${SUDO} chmod +x "$cli"
  ok "管理命令已就位：$cli"
  case ":$PATH:" in *":$bindir:"*) : ;; *) warn "$bindir 不在 PATH，可用绝对路径 $cli" ;; esac
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
  printf "  %s管理员密码%s  %s%s%s\n" "$DIM" "$RST" "$YELLOW" "$ADMIN_PASS" "$RST"
  printf "  %s访问模式%s    %s\n" "$DIM" "$RST" "$ACCESS_MODE"
  printf "  %s安装目录%s    %s\n" "$DIM" "$RST" "$INSTALL_DIR"
  hr
  printf "  %s后续管理（%ssimpleurl --help%s%s 看全部）%s\n" "$BOLD" "$DIM" "$RST" "$BOLD" "$RST"
  printf "    simpleurl status      看状态\n"
  printf "    simpleurl log         看日志\n"
  printf "    simpleurl upgrade     升级到最新代码\n"
  printf "    simpleurl backup      备份数据\n"
  printf "    simpleurl password    重置管理员密码\n"
  hr
  [ -n "$PREV_BACKUP" ] && printf "  %s上一版数据已备份到 %s（只留最近 5 份）%s\n" "$DIM" "$PREV_BACKUP" "$RST"
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
  if need_cmd systemctl; then
    local SUDO=""; [ "$(id -u)" != "0" ] && need_cmd sudo && SUDO="sudo"
    $SUDO systemctl stop "$SERVICE_NAME" >/dev/null 2>&1 || true
    $SUDO systemctl disable "$SERVICE_NAME" >/dev/null 2>&1 || true
    $SUDO rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
    $SUDO systemctl daemon-reload >/dev/null 2>&1 || true
  fi
  if [ -f "$INSTALL_DIR/simpleurl.pid" ]; then
    kill "$(cat "$INSTALL_DIR/simpleurl.pid" 2>/dev/null)" 2>/dev/null || true
  fi
  rm -f /usr/local/bin/simpleurl "$HOME/.local/bin/simpleurl" 2>/dev/null || true
  # 卸载是唯一「删整个目录」的地方：路径异常（空 / / / $HOME）时宁可不删
  case "$INSTALL_DIR" in
    ""|"/"|"$HOME"|"$HOME/"|"/root"|"/usr"|"/usr/local") die "安装目录 ${INSTALL_DIR} 不像本脚本装的，拒绝删除" ;;
  esac
  rm -rf "${INSTALL_DIR:?}"
  ok "已卸载干净"
}

# ---------------------------------------------------------------- 主流程
# 读已有安装的配置：升级时原样沿用，不重新提问
load_existing_env() { # envfile
  local f="$1" raw_token
  SITE_NAME="$(grep -E '^SITE_NAME=' "$f" | head -1 | cut -d= -f2- || echo "$SITE_NAME")"
  PORT="$(grep -E '^PORT=' "$f" | head -1 | cut -d= -f2- || echo "$PORT")"
  PUBLIC_URL="$(grep -E '^PUBLIC_URL=' "$f" | head -1 | cut -d= -f2- || echo "")"
  ACCESS_MODE="$(grep -E '^ACCESS_MODE=' "$f" | head -1 | cut -d= -f2- || echo "public")"
  SECRET_KEY="$(grep -E '^SECRET_KEY=' "$f" | head -1 | cut -d= -f2- || echo "")"
  raw_token="$(grep -E '^ADMIN_TOKEN=' "$f" | head -1 | cut -d= -f2- || echo "")"
  case "$raw_token" in
    *:*) ADMIN_USER="${raw_token%%:*}"; ADMIN_PASS="${raw_token#*:}" ;;
    "")  ADMIN_USER="admin"; ADMIN_PASS="" ;;
    *)   ADMIN_USER="admin"; ADMIN_PASS="$raw_token" ;;
  esac
}

do_install() {
  # banner 只打一次：main 里已经打过，别再重复刷屏
  [ "$BANNER_SHOWN" = "y" ] || { banner; BANNER_SHOWN="y"; }

  # 已有安装：沿用配置，别把人已经配好的东西再问一遍
  local existing_env="$INSTALL_DIR/.env"
  local reuse="n"
  local prev_runtime=""
  [ -f "$MARK_FILE" ] && prev_runtime="$(sed -n 's/^runtime=//p' "$MARK_FILE" | head -1)"

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

  doctor || die "环境体检没过，按上面的提示处理后再跑一次"

  # 升级前先看一眼远端版本：已经最新就不折腾了，别每次都重装依赖、重启服务。
  # 注意命令顺序：函数调用放最后，「函数返回 1」就不会把整个 if 判成失败。
  # 升级时换掉安装目录里的脚本自己：否则这台机器永远用装那天的脚本去升代码。
  if [ "$ACTION_UPGRADE" = "y" ] && [ "$reuse" = "y" ] && [ "$SCRIPT_REEXEC" != "y" ] \
     && [ "$SCRIPT_FETCHED" != "y" ]; then
    local fresh_script; fresh_script="$(fetch_latest_script)"
    if [ -n "$fresh_script" ]; then
      SCRIPT_URL_HINT="$fresh_script"
      if ! cmp -s "$fresh_script" "$INSTALL_DIR/install.sh" 2>/dev/null; then
        ok "安装脚本可更新，换成远端最新版再继续"
        # 换壳重跑：本进程还带着旧脚本已经解析好的逻辑，直接往下跑等于没换
        SCRIPT_REEXEC="y"
        SIMPLEURL_HOME="$INSTALL_DIR" SCRIPT_REEXEC=y \
          exec bash "$fresh_script" upgrade --yes
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
      ok "已经是最新代码（v$REMOTE_PEEK），服务不动"
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

    ask "访问模式 public 谁都能建 / admin 仅管理员 / invite 邀请码" "public"; ACCESS_MODE="$ANSWER"
    case "$ACCESS_MODE" in public|admin|invite) : ;; *) ACCESS_MODE="public" ;; esac
    SECRET_KEY="$(gen_key 32)"
  else
    [ -n "${ADMIN_PASS:-}" ] || ADMIN_PASS="$(gen_password)"
    [ -n "${SECRET_KEY:-}" ] || SECRET_KEY="$(gen_key 32)"
    [ -n "${ACCESS_MODE:-}" ] || ACCESS_MODE="public"
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
    write_env "$existing_env"
    write_start_wrapper
    if ! setup_systemd; then
      if ! start_background; then
        # systemd 环境里注册失败了但进程可能还在，先清干净再回滚
        stop_running
        rollback_or_die "服务没能启动"
      fi
    fi
  fi

  printf 'version=%s\ncode_version=%s\nruntime=%s\nport=%s\ninstalled_at=%s\n' \
    "$VERSION" "$(remote_version "$INSTALL_DIR")" "$RUNTIME" "$PORT" \
    "$(date '+%Y-%m-%d %H:%M:%S')" > "$MARK_FILE"
  install_cli
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
