#!/usr/bin/env bash
# SimpleURL 一键安装 / 管理脚本
#
#   交互安装   curl -fsSL <安装地址> | bash
#   全默认     ./install.sh --yes          不提问，密码自动生成
#   重装/升级  ./install.sh                已有安装会沿用配置，只更新代码
#   卸载       ./install.sh --uninstall
#
# 有 Docker 用 Docker，没有就用 Python venv。国内网络自动启用镜像加速。
# 装完给一个 `simpleurl` 命令管后续：status / log / password / backup / update ...
set -euo pipefail

VERSION="1.1.0"
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo "")"

# ---------------------------------------------------------------- 常量
# 默认从当前仓库安装；换镜像源只改这两个变量
GIT_HOST="${GIT_HOST:-https://cnb.cool}"
REPO_SLUG="${REPO_SLUG:-broccoli_xi/TechSauce/SimpleURL}"
REPO_BRANCH="${REPO_BRANCH:-main}"
REPO_URL="${REPO_URL:-$GIT_HOST/$REPO_SLUG.git}"
# 归档地址按平台区分：CNB 是 /-/archive/<分支>.tar.gz
# 归档地址按平台区分：CNB 是 /-/archive/<分支>.tar.gz
REPO_TARBALL="${REPO_TARBALL:-$GIT_HOST/$REPO_SLUG/-/archive/$REPO_BRANCH.tar.gz}"
# 源码加速前缀：只对 GitHub 有用。CNB / Gitee 本身在国内，
# 再套一层加速反而多一跳（实测还会 403），所以按域名自动判断要不要用。
GIT_MIRROR="${GIT_MIRROR:-https://ghfast.top/}"

INSTALL_DIR="${SIMPLEURL_HOME:-$HOME/simpleurl}"
PORT="${PORT:-8000}"
SERVICE_NAME="${SERVICE_NAME:-simpleurl}"
SITE_NAME="${SITE_NAME:-SimpleURL}"
USE_CN_MIRROR="${USE_CN_MIRROR:-auto}"     # auto | 1 | 0
PIP_MIRRORS="${PIP_MIRRORS:-https://pypi.tuna.tsinghua.edu.cn/simple https://mirrors.aliyun.com/pypi/simple https://mirrors.cloud.tencent.com/pypi/simple https://mirrors.ustc.edu.cn/pypi/simple}"
DOCKER_MIRRORS="${DOCKER_MIRRORS:-https://docker.1ms.run,https://docker.m.daocloud.io,https://docker.xuanyuan.me,https://dockerproxy.net}"
LOG_FILE="$INSTALL_DIR/simpleurl.log"
MARK_FILE="$INSTALL_DIR/.simpleurl-managed"   # 有它才认作本脚本装的目录（内含运行方式等）

RUNTIME=""          # docker | python，安装时定，simpleurl 命令会读它
INTERACTIVE=1
ACTION="install"
ASSUME_YES="n"

while [ $# -gt 0 ]; do
  case "$1" in
    --yes|-y)      INTERACTIVE=0 ;;
    --uninstall)   ACTION="uninstall"; INTERACTIVE=0 ;;
    --force)       ASSUME_YES="y" ;;
    --help|-h)     ACTION="help" ;;
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

  ./install.sh              交互安装（问站点名、端口、域名、账号密码）
  ./install.sh --yes        全默认安装，密码自动生成
  ./install.sh --uninstall  卸载（会二次确认）
  ./install.sh --help       这段说明

环境变量（都可选）：
  SIMPLEURL_HOME   安装目录，默认 ~/simpleurl
  PORT             监听端口，默认 8000
  SITE_NAME        站点名称
  USE_CN_MIRROR    auto（默认）/ 1 强制国内加速 / 0 用官方源
  PIP_INDEX_URL    指定 pip 源
  DOCKER_MIRRORS   逗号分隔的 Docker 镜像加速地址
  REPO_URL / REPO_TARBALL / GIT_HOST / REPO_SLUG   换安装来源

装完用 simpleurl 管理：
  simpleurl status | start | stop | restart | log
  simpleurl url | password | config | backup | update | uninstall
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
  if [ -n "${PIP_INDEX_URL:-}" ] && curl -fsS -m 3 -o /dev/null "$PIP_INDEX_URL" 2>/dev/null; then
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

fetch() { # url out
  if need_cmd curl; then curl -fL --retry 2 --connect-timeout 10 -m 300 -o "$2" "$1"
  elif need_cmd wget; then wget -q -T 30 -O "$2" "$1"
  else die "需要 curl 或 wget"; fi
}

# ---------------------------------------------------------------- 源码
# 换代码前先把数据搬出来：数据是这个站唯一带不走的东西，代码随时能重下。
backup_data() {
  local stamp; stamp="$(date +%Y%m%d-%H%M%S)"
  local dir="$INSTALL_DIR/.backup/$stamp"
  mkdir -p "$dir"
  [ -f "$INSTALL_DIR/simpleurl.db" ] && cp -a "$INSTALL_DIR/simpleurl.db"* "$dir/" 2>/dev/null || true
  [ -f "$INSTALL_DIR/.env" ] && cp -a "$INSTALL_DIR/.env" "$dir/" 2>/dev/null || true
  [ -d "$dir" ] && [ -n "$(ls -A "$dir" 2>/dev/null)" ] && ok "旧数据已备份：$dir"
}

# 源码目录里认得的顶层条目：换代码时只管这些，其它（.venv/.env/数据/备份）一概不碰
# 注意不含 install.sh：它自己就是入口，覆盖成远端的旧版会让人下次跑到旧脚本
SOURCE_ENTRIES="app requirements.txt requirements-dev.txt .env.example Dockerfile docker-compose.yml
.dockerignore .gitignore pytest.ini run.sh scripts README.md .cnb.yml"

clear_source() {
  local entry
  for entry in $SOURCE_ENTRIES; do
    rm -rf "${INSTALL_DIR:?}/$entry"
  done
}

is_tarball() { # file：先认 gzip 魔数，比 `file` 稳，也不依赖它
  [ -s "$1" ] || return 1
  [ "$(head -c 2 "$1" 2>/dev/null)" = "$(printf '\037\213')" ]
}

install_source() {
  mkdir -p "$INSTALL_DIR"
  local src_dir; src_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo "")"

  # 脚本就在源码目录里跑：直接复用，不联网
  if [ -n "$src_dir" ] && [ -f "$src_dir/app/main.py" ] \
     && [ "$src_dir" = "$(cd "$INSTALL_DIR" && pwd)" ]; then
    ok "使用本地源码：$INSTALL_DIR"; return 0
  fi

  if [ -d "$INSTALL_DIR/.git" ]; then
    step "更新已有安装的代码（git）"
    if git -C "$INSTALL_DIR" fetch --depth 1 origin "$REPO_BRANCH" >/dev/null 2>&1 \
       && git -C "$INSTALL_DIR" reset --hard FETCH_HEAD >/dev/null 2>&1; then
      ok "代码已更新"; return 0
    fi
    warn "git 拉取失败，改用源码包"
  fi

  [ -f "$INSTALL_DIR/app/main.py" ] && backup_data

  step "下载源码"
  local urls=() u tarball="$INSTALL_DIR/.src.tar.gz"
  # 镜像地址与官方地址不同才两路都试，否则会白等一遍同样的失败
  [ "$(mirror_url "$REPO_TARBALL")" != "$REPO_TARBALL" ] && urls+=("$(mirror_url "$REPO_TARBALL")")
  urls+=("$REPO_TARBALL")
  for u in ${urls[@]+"${urls[@]}"}; do
    rm -f "$tarball"
    if fetch "$u" "$tarball" >/dev/null 2>&1 && is_tarball "$tarball"; then
      # 解到临时目录再替换：解压半路失败时，现有代码还是完整的
      local stage="$INSTALL_DIR/.stage"
      rm -rf "$stage"; mkdir -p "$stage"
      if tar -xzf "$tarball" -C "$stage" --strip-components=1 2>/dev/null && [ -f "$stage/app/main.py" ]; then
        clear_source
        cp -a "$stage/." "$INSTALL_DIR/"
        rm -rf "$stage" "$tarball"
        ok "源码已就绪"; return 0
      fi
      rm -rf "$stage"
      warn "源码包解压失败，换下一个地址"
    fi
    warn "源码包地址不可用：$u"
    rm -f "$tarball"
  done

  if need_cmd git; then
    local clone_url; clone_url="$(mirror_url "$REPO_URL")"
    local tmp="$INSTALL_DIR/.clone"
    rm -rf "$tmp"
    if git clone --depth 1 --branch "$REPO_BRANCH" "$clone_url" "$tmp" >/dev/null 2>&1; then
      clear_source
      cp -a "$tmp/." "$INSTALL_DIR/"
      rm -rf "$tmp"
      ok "源码已通过 git 就绪"; return 0
    fi
    rm -rf "$tmp"
  fi
  die "源码下载失败。可手动下载后解压到 $INSTALL_DIR 再重跑本脚本"
}

# ---------------------------------------------------------------- Docker
have_docker() { need_cmd docker && docker info >/dev/null 2>&1; }

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

install_docker_mode() {
  step "Docker 方式部署"
  configure_docker_mirror
  local build_args=()
  if [ "$CN_MIRROR_ON" = "1" ] && [ -n "$PIP_INDEX_URL" ]; then
    build_args=(--build-arg "PIP_INDEX_URL=$PIP_INDEX_URL" --build-arg "PIP_TRUSTED_HOST=$PIP_TRUSTED_HOST")
  fi
  docker_pull "python:3.12-slim" || warn "基础镜像拉取受阻，继续尝试构建"
  step "构建镜像（首次会装依赖）"
  docker build ${build_args[@]+"${build_args[@]}"} -t simpleurl:latest -f "$INSTALL_DIR/Dockerfile" "$INSTALL_DIR" >/dev/null \
    || die "镜像构建失败。可试着 USE_CN_MIRROR=0 重跑，或检查网络"
  docker rm -f "$SERVICE_NAME" >/dev/null 2>&1 || true
  docker volume create "${SERVICE_NAME}-data" >/dev/null
  docker run -d --name "$SERVICE_NAME" --restart unless-stopped \
    -p "${PORT}:8000" -v "${SERVICE_NAME}-data:/data" \
    -e "SITE_NAME=$SITE_NAME" -e "PORT=8000" \
    -e "DATABASE_URL=sqlite:////data/simpleurl.db" \
    -e "SECRET_KEY=$SECRET_KEY" \
    -e "ADMIN_TOKEN=$ADMIN_USER:$ADMIN_PASS" \
    -e "PUBLIC_URL=$PUBLIC_URL" -e "ACCESS_MODE=$ACCESS_MODE" \
    -e "SIMPLEURL_ENV_FILE=/data/.env" \
    --label "simpleurl.managed=1" \
    simpleurl:latest >/dev/null || die "容器启动失败"
  RUNTIME="docker"
  ok "容器已启动：$SERVICE_NAME（数据卷 ${SERVICE_NAME}-data）"
  # 让 simpleurl password / config 有可改的配置文件
  docker exec "$SERVICE_NAME" sh -c "printf '%s\n' \
"SITE_NAME=$SITE_NAME" "PORT=8000" "PUBLIC_URL=$PUBLIC_URL" \
"SECRET_KEY=$SECRET_KEY" "ADMIN_TOKEN=$ADMIN_USER:$ADMIN_PASS" \
> /data/.env && chmod 600 /data/.env" >/dev/null 2>&1 || true
  docker restart "$SERVICE_NAME" >/dev/null 2>&1 || true
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

install_python_mode() {
  step "Python 方式部署（venv）"
  ensure_python
  if ! python3 -m venv "$INSTALL_DIR/.venv" 2>/dev/null; then
    warn "创建 venv 失败，补装 python3-venv"
    pkg_install python3-venv python3-pip || true
    python3 -m venv "$INSTALL_DIR/.venv" || die "创建虚拟环境失败，请手动安装 python3-venv"
  fi
  local pip="$INSTALL_DIR/.venv/bin/pip"
  step "安装依赖（通常 1~2 分钟）"
  pip_install "$pip" --upgrade pip >/dev/null 2>&1 || true
  pip_install "$pip" -r "$INSTALL_DIR/requirements.txt" || die "依赖安装失败，检查网络后重试"
  # 装完自检：缺包在这里说清楚，比等 uvicorn 抛一屏 import 栈好读
  if ! "$pip" check >/dev/null 2>&1; then
    warn "依赖有冲突，仍继续启动（可用 simpleurl log 查详情）"
  fi
  if ! "$INSTALL_DIR/.venv/bin/python" -c "import fastapi, uvicorn, httpx" 2>/dev/null; then
    warn "依赖没装全，正在用官方源补装一次"
    "$pip" install -q --disable-pip-version-check -r "$INSTALL_DIR/requirements.txt" \
      || die "依赖装不上。可手改 $INSTALL_DIR/requirements.txt 后重跑，或指定 PIP_INDEX_URL=<你的源>"
  fi
  RUNTIME="python"
}

write_env() {
  local file="$1"
  umask 077
  cat > "$file" <<EOF
# SimpleURL 配置，由 install.sh $VERSION 生成于 $(date '+%Y-%m-%d %H:%M:%S')
# 改完执行 simpleurl restart 生效
SITE_NAME=$SITE_NAME
HOST=0.0.0.0
PORT=$PORT
PUBLIC_URL=$PUBLIC_URL
ACCESS_MODE=$ACCESS_MODE
DATABASE_URL=$DATABASE_URL
SECRET_KEY=$SECRET_KEY
# 后台凭据：admin:密码 表示账号密码分开设；只写口令则账号固定 admin
ADMIN_TOKEN=$ADMIN_USER:$ADMIN_PASS
EOF
  chmod 600 "$file" 2>/dev/null || true
}

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

start_background() {
  step "后台启动服务"
  if need_cmd setsid; then
    setsid nohup "$INSTALL_DIR/start.sh" >>"$LOG_FILE" 2>&1 </dev/null &
  else
    nohup "$INSTALL_DIR/start.sh" >>"$LOG_FILE" 2>&1 </dev/null &
  fi
  echo $! > "$INSTALL_DIR/simpleurl.pid"
  sleep 2
  local pid; pid="$(cat "$INSTALL_DIR/simpleurl.pid" 2>/dev/null || echo "")"
  # start.sh 以 exec 收尾，所以这个 PID 就是真进程，脚本退出也找得到
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    ok "已启动，PID $pid"
  else
    warn "进程没起来，最近日志："; tail -n 10 "$LOG_FILE" 2>/dev/null | sed 's/^/    /'
    return 1
  fi
}

setup_systemd() {
  [ "$RUNTIME" = "python" ] || return 1
  need_cmd systemctl || return 1
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
  elif need_cmd systemctl && [ -f "/etc/systemd/system/${SERVICE_NAME}.service" ]; then
    local SUDO=""; [ "$(id -u)" != "0" ] && need_cmd sudo && SUDO="sudo"
    $SUDO systemctl stop "$SERVICE_NAME" >/dev/null 2>&1 || true
  elif [ -f "$INSTALL_DIR/simpleurl.pid" ]; then
    kill "$(cat "$INSTALL_DIR/simpleurl.pid")" 2>/dev/null || true
    rm -f "$INSTALL_DIR/simpleurl.pid"
  fi
}

check_port_free() {
  local pid=""
  if need_cmd ss; then pid="$(ss -lntp 2>/dev/null | awk -v p=":$PORT" '$4 ~ p"$" {print $NF}' | head -1)"
  elif need_cmd lsof; then pid="$(lsof -ti "tcp:$PORT" 2>/dev/null | head -1)"; fi
  [ -n "$pid" ] || return 0
  warn "端口 $PORT 被占用：$pid"
  ask "换个端口（留空沿用 $PORT）" ""
  [ -n "$ANSWER" ] && PORT="$ANSWER"
}

wait_health() {
  step "等待服务就绪"
  local i=0 max="${1:-60}" code="000"
  need_cmd curl || { sleep 3; return 0; }
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

# ---------------------------------------------------------------- simpleurl 命令
install_cli() {
  local bindir="/usr/local/bin" SUDO=""
  if [ "$(id -u)" != "0" ]; then
    if need_cmd sudo && sudo -n true 2>/dev/null; then SUDO="sudo"
    else bindir="$HOME/.local/bin"; mkdir -p "$bindir"; fi
  fi
  [ -n "$SUDO" ] && $SUDO mkdir -p "$bindir" || mkdir -p "$bindir"
  local cli="$bindir/simpleurl"
  ${SUDO} tee "$cli" >/dev/null <<EOF
#!/usr/bin/env bash
# SimpleURL 管理命令（install.sh $VERSION 生成）
set -uo pipefail
DIR="$INSTALL_DIR"
NAME="$SERVICE_NAME"
PORT="$PORT"
RUNTIME="$RUNTIME"
URL_HINT="$PUBLIC_URL"
G=\$'\033[32m'; R=\$'\033[31m'; D=\$'\033[2m'; N=\$'\033[0m'

env_get() { grep -E "^\$1=" "\$DIR/.env" 2>/dev/null | head -1 | cut -d= -f2-; }
say() { printf '%s\n' "\$*"; }

running() {
  if [ "\$RUNTIME" = "docker" ]; then
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "\$NAME"
  elif command -v systemctl >/dev/null 2>&1 && [ -f "/etc/systemd/system/\$NAME.service" ]; then
    systemctl is-active --quiet "\$NAME"
  else
    [ -f "\$DIR/simpleurl.pid" ] && kill -0 "\$(cat "\$DIR/simpleurl.pid")" 2>/dev/null
  fi
}

site_url() {
  [ -n "\$URL_HINT" ] && { printf '%s' "\$URL_HINT"; return; }
  local ip; ip="\$(hostname -I 2>/dev/null | awk '{print \$1}')"
  printf 'http://%s:%s' "\${ip:-127.0.0.1}" "\$PORT"
}

do_start() {
  if running; then say "已在运行"; return 0; fi
  if [ "\$RUNTIME" = "docker" ]; then
    docker start "\$NAME" >/dev/null && say "已启动容器 \$NAME"
  elif command -v systemctl >/dev/null 2>&1 && [ -f "/etc/systemd/system/\$NAME.service" ]; then
    sudo systemctl start "\$NAME" && say "已启动 systemd 服务 \$NAME"
  else
    setsid nohup "\$DIR/start.sh" >>"\$DIR/simpleurl.log" 2>&1 </dev/null &
    echo \$! > "\$DIR/simpleurl.pid"
    say "已后台启动，PID \$(cat "\$DIR/simpleurl.pid")"
  fi
}

do_stop() {
  if [ "\$RUNTIME" = "docker" ]; then
    docker stop "\$NAME" >/dev/null 2>&1 && say "已停止容器 \$NAME" || say "容器未运行"
  elif command -v systemctl >/dev/null 2>&1 && [ -f "/etc/systemd/system/\$NAME.service" ]; then
    sudo systemctl stop "\$NAME" >/dev/null 2>&1 && say "已停止 \$NAME"
  elif [ -f "\$DIR/simpleurl.pid" ]; then
    kill "\$(cat "\$DIR/simpleurl.pid")" 2>/dev/null && say "已停止"
    rm -f "\$DIR/simpleurl.pid"
  else
    say "未在运行"
  fi
}

set_password() {
  local pass="\${1:-}"
  [ -n "\$pass" ] || pass="\$(openssl rand -base64 32 2>/dev/null | tr -dc 'A-Za-z2-9' | cut -c1-16)"
  local cred="admin:\$pass"
  if [ "\$RUNTIME" = "docker" ]; then
    docker exec "\$NAME" python - <<'PY' "\$cred"
import pathlib, re, sys
p = pathlib.Path('/data/.env')
text = p.read_text() if p.exists() else ''
text = re.sub(r'^ADMIN_TOKEN=.*\$', '', text, flags=re.M).rstrip('\n')
p.write_text(text + '\nADMIN_TOKEN=' + sys.argv[1] + '\n')
PY
    docker restart "\$NAME" >/dev/null 2>&1 || true
  else
    sed -i.bak "s|^ADMIN_TOKEN=.*|ADMIN_TOKEN=\$cred|" "\$DIR/.env" 2>/dev/null \
      || sed -i '' "s|^ADMIN_TOKEN=.*|ADMIN_TOKEN=\$cred|" "\$DIR/.env"
    rm -f "\$DIR/.env.bak"
    do_stop; sleep 1; do_start
  fi
  say "新密码：\$pass"
  say "账号：admin　后台：\$(site_url)/admin"
}

case "\${1:-help}" in
  start)   do_start ;;
  stop)    do_stop ;;
  restart) do_stop; sleep 1; do_start ;;
  status)
    if running; then printf "%s● 运行中%s\n" "\$G" "\$N"
    else printf "%s● 已停止%s\n" "\$R" "\$N"; fi
    if command -v curl >/dev/null 2>&1; then
      code=\$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:\$PORT/healthz" || echo 000)
      [ "\$code" = "200" ] && printf "  健康检查 %s200 OK%s\n" "\$G" "\$N" \
                           || printf "  健康检查 %s%s%s\n" "\$R" "\$code" "\$N"
    fi
    printf "  站点 %s\n  后台 %s/admin\n" "\$(site_url)" "\$(site_url)"
    [ "\$RUNTIME" = "docker" ] && docker ps --filter "name=^\$NAME\$" --format '  容器 {{.Status}} · {{.Ports}}' ;;
  url)      site_url; echo ;;
  log)
    if [ "\$RUNTIME" = "docker" ]; then docker logs --tail 100 -f "\$NAME"
    else tail -n 100 -f "\$DIR/simpleurl.log"; fi ;;
  password) set_password "\${2:-}" ;;
  config)
    if [ "\$RUNTIME" = "docker" ]; then
      docker exec "\$NAME" cat /data/.env
      say ""
      say "（Docker 模式改配置：docker exec -it \$NAME vi /data/.env 后 simpleurl restart）"
    else
      "\${EDITOR:-vi}" "\$DIR/.env"
    fi ;;
  backup)
    out="\${2:-\$HOME/simpleurl-backup-\$(date +%Y%m%d-%H%M%S).tar.gz}"
    if [ "\$RUNTIME" = "docker" ]; then
      docker run --rm -v "\${NAME}-data:/data" -v "\$(dirname "\$out"):/backup" alpine \\
        tar -czf "/backup/\$(basename "\$out")" -C /data . >/dev/null && say "已备份到 \$out"
    else
      tar -czf "\$out" -C "\$DIR" simpleurl.db .env 2>/dev/null && say "已备份到 \$out"
    fi ;;
  update)  say "重新执行安装脚本以更新：\$DIR/install.sh" ;;
  uninstall) exec "\$DIR/install.sh" --uninstall ;;
  help|--help|-h|*)
    cat <<USAGE
SimpleURL 管理命令

  simpleurl status              运行状态与健康检查
  simpleurl start | stop | restart
  simpleurl log                 跟踪日志
  simpleurl url                 打印站点与后台地址
  simpleurl password [新密码]    重置管理员密码（不给参数就随机生成）
  simpleurl config              查看 / 编辑配置
  simpleurl backup [路径]        备份数据库与配置
  simpleurl update              重新执行安装脚本更新
  simpleurl uninstall           卸载
USAGE
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
  printf "    simpleurl restart     重启\n"
  printf "    simpleurl password    重置管理员密码\n"
  printf "    simpleurl backup      备份数据\n"
  hr
  printf "  %s密码请立刻抄走。之后可在后台「设置 → 管理员账号」改，改完重启不丢。%s\n" "$YELLOW" "$RST"
  [ -z "$PUBLIC_URL" ] && printf "  %s想让短链显示成自己的域名？在 %s/.env 里设 PUBLIC_URL=https://s.example.com 再 restart。%s\n" "$DIM" "$INSTALL_DIR" "$RST"
  printf "  %sPowered by SimpleURL — https://cnb.cool/%s%s\n\n" "$DIM" "$REPO_SLUG" "$RST"
}

do_uninstall() {
  step "卸载 SimpleURL"
  if [ ! -f "$MARK_FILE" ]; then
    die "$INSTALL_DIR 不是本脚本装的目录，为安全起见不自动删"
  fi
  ask_yes "确认删除 $INSTALL_DIR 与全部数据？（不可恢复）" "n"
  [ "$ANSWER" = "y" ] || die "已取消（非交互请加 --force）"
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
  rm -f /usr/local/bin/simpleurl "$HOME/.local/bin/simpleurl" 2>/dev/null || true
  rm -rf "$INSTALL_DIR"
  ok "已卸载干净"
}

# ---------------------------------------------------------------- 主流程
main() {
  case "$ACTION" in
    help) usage; exit 0 ;;
    uninstall) banner; do_uninstall; exit 0 ;;
  esac

  banner
  probe_network
  if [ "$CN_MIRROR_ON" = "1" ]; then
    ok "网络探测：国内线路，启用加速（pip${PIP_INDEX_URL:+ · $PIP_INDEX_URL}）"
  else
    ok "网络探测：直连官方源"
  fi

  # 已有安装的话，沿用原来的配置，只提醒
  local existing_env="$INSTALL_DIR/.env"
  local reuse="n"
  if [ -f "$existing_env" ]; then
    reuse="y"
    step "检测到已有安装：$INSTALL_DIR（沿用现有配置，只更新代码）"
    SITE_NAME="$(grep -E '^SITE_NAME=' "$existing_env" | head -1 | cut -d= -f2- || echo "$SITE_NAME")"
    PORT="$(grep -E '^PORT=' "$existing_env" | head -1 | cut -d= -f2- || echo "$PORT")"
    PUBLIC_URL="$(grep -E '^PUBLIC_URL=' "$existing_env" | head -1 | cut -d= -f2- || echo "")"
    ACCESS_MODE="$(grep -E '^ACCESS_MODE=' "$existing_env" | head -1 | cut -d= -f2- || echo "public")"
    SECRET_KEY="$(grep -E '^SECRET_KEY=' "$existing_env" | head -1 | cut -d= -f2- || echo "")"
    local raw_token; raw_token="$(grep -E '^ADMIN_TOKEN=' "$existing_env" | head -1 | cut -d= -f2- || echo "")"
    case "$raw_token" in
      *:*) ADMIN_USER="${raw_token%%:*}"; ADMIN_PASS="${raw_token#*:}" ;;
      "")  ADMIN_USER="admin"; ADMIN_PASS="" ;;
      *)   ADMIN_USER="admin"; ADMIN_PASS="$raw_token" ;;
    esac
  else
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
    else
      warn "未填域名，装完用 http://<服务器IP>:$PORT 访问"
    fi

    ask "管理员账号" "admin"; ADMIN_USER="${ANSWER:-admin}"
    ask_password "管理员密码"
    ADMIN_PASS="${ANSWER:-$(gen_password)}"

    ask "访问模式 public 谁都能建 / admin 仅管理员 / invite 邀请码" "public"; ACCESS_MODE="$ANSWER"
    case "$ACCESS_MODE" in public|admin|invite) : ;; *) ACCESS_MODE="public" ;; esac
    SECRET_KEY="$(gen_key 32)"
  fi

  echo
  install_source

  if have_docker; then
    [ "$reuse" = "y" ] && stop_running
    install_docker_mode
  else
    warn "没有可用的 Docker，改用 Python 直装（更省资源）"
    stop_running
    install_python_mode
    DATABASE_URL="sqlite:///$INSTALL_DIR/simpleurl.db"
    write_env "$existing_env"
    write_start_wrapper
    setup_systemd || start_background
  fi

  printf 'version=%s\nruntime=%s\nport=%s\ninstalled_at=%s\n' \
    "$VERSION" "$RUNTIME" "$PORT" "$(date '+%Y-%m-%d %H:%M:%S')" > "$MARK_FILE"
  install_cli
  wait_health 60 || warn "服务已启动但健康检查未过，用 simpleurl log 看原因"
  summary
}

main "$@"
