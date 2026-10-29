#!/usr/bin/env sh
# SimpleURL 一键安装脚本
# 用法： curl -fsSL https://raw.githubusercontent.com/techjiang/SimpleURL/main/install.sh | sh
# 或：   wget -O simpleurl-install.sh <同上地址> && chmod +x simpleurl-install.sh && ./simpleurl-install.sh
#
# 支持 Linux（含 WSL）与 macOS；Docker 已装则用 Docker，否则用 Python venv。
set -eu

REPO_URL="https://github.com/techjiang/SimpleURL.git"
REPO_TARBALL="https://github.com/techjiang/SimpleURL/archive/refs/heads/main.tar.gz"
INSTALL_DIR="${SIMPLEURL_HOME:-$HOME/simpleurl}"
PORT="${PORT:-8000}"
SERVICE_NAME="${SERVICE_NAME:-simpleurl}"

RED="\033[31m"; GREEN="\033[32m"; YELLOW="\033[33m"; BLUE="\033[36m"; DIM="\033[2m"; RST="\033[0m"
info() { printf "${BLUE}▸${RST} %s\n" "$*"; }
ok()   { printf "${GREEN}✓${RST} %s\n" "$*"; }
warn() { printf "${YELLOW}!${RST} %s\n" "$*"; }
die()  { printf "${RED}✗${RST} %s\n" "$*" >&2; exit 1; }

banner() {
  printf "\n${GREEN}SimpleURL${RST} ${DIM}· 轻量、流畅、好用的短链接系统${RST}\n"
  printf "${DIM}一个链接，短到可以记住。${RST}\n\n"
}

need_cmd() { command -v "$1" >/dev/null 2>&1; }

have_docker() { need_cmd docker && docker info >/dev/null 2>&1; }

ensure_git() {
  if need_cmd git; then return 0; fi
  warn "未检测到 git，尝试安装…"
  if need_cmd apt-get; then apt-get update -qq && apt-get install -y -qq git curl
  elif need_cmd apk; then apk add --no-cache git curl
  elif need_cmd dnf; then dnf install -y git curl
  elif need_cmd yum; then yum install -y git curl
  elif need_cmd brew; then brew install git
  else die "无法自动安装 git，请手动安装后重试"; fi
}

ensure_python() {
  if need_cmd python3; then return 0; fi
  warn "未检测到 python3，尝试安装…"
  if need_cmd apt-get; then apt-get update -qq && apt-get install -y -qq python3 python3-venv python3-pip
  elif need_cmd apk; then apk add --no-cache python3 py3-pip
  elif need_cmd dnf; then dnf install -y python3 python3-pip
  elif need_cmd brew; then brew install python
  else die "无法自动安装 python3，请手动安装后重试"; fi
}

install_venv_pkg() {
  if need_cmd apt-get; then
    apt-get update -qq
    apt-get install -y -qq "python$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')-venv" \
      || apt-get install -y -qq python3-venv
  elif need_cmd apk; then apk add --no-cache python3 py3-pip
  elif need_cmd dnf; then dnf install -y python3-pip
  elif need_cmd brew; then :  # brew 的 python 自带 venv
  else return 1; fi
}

gen_key() {
  if need_cmd openssl; then openssl rand -hex "$1"
  else python3 -c "import secrets;print(secrets.token_hex($1))"; fi
}

# ---------- 拉取源码 ----------
fetch_source() {
  if [ -f "$INSTALL_DIR/install.sh" ] && [ -d "$INSTALL_DIR/app" ] && [ ! -d "$INSTALL_DIR/.git" ]; then
    info "复用已有目录 $INSTALL_DIR（非 git 安装，跳过拉取）"
  elif [ -d "$INSTALL_DIR/.git" ]; then
    info "检测到已有安装，更新代码…"
    git -C "$INSTALL_DIR" fetch --depth 1 origin main >/dev/null 2>&1 || true
    git -C "$INSTALL_DIR" reset --hard origin/main >/dev/null 2>&1 || true
  else
    info "下载源码到 $INSTALL_DIR"
    mkdir -p "$INSTALL_DIR"
    if ensure_git && git clone --depth 1 "$REPO_URL" "$INSTALL_DIR" >/dev/null 2>&1; then
      ok "git 克隆完成"
    else
      warn "git 克隆失败，改用源码包下载"
      need_cmd curl || die "需要 curl 或 wget 与 tar"
      curl -fsSL "$REPO_TARBALL" | tar -xz -C "$INSTALL_DIR" --strip-components=1
    fi
  fi
}

# ---------- Docker 模式 ----------
install_docker_mode() {
  info "使用 Docker 模式部署"
  if ! docker image inspect simpleurl:latest >/dev/null 2>&1; then
    info "构建镜像（首次较慢）…"
    docker build -t simpleurl:latest "$INSTALL_DIR"
  fi
  if docker container inspect "$SERVICE_NAME" >/dev/null 2>&1; then
    info "重建容器以应用最新代码"
    docker rm -f "$SERVICE_NAME" >/dev/null 2>&1 || true
  fi
  docker volume create "$SERVICE_NAME-data" >/dev/null
  SECRET=$(gen_key 32); ADMIN=$(gen_key 16)
  docker run -d --name "$SERVICE_NAME" \
    --restart unless-stopped \
    -p "${PORT}:8000" \
    -v "$SERVICE_NAME-data:/data" \
    -e SECRET_KEY="$SECRET" \
    -e ADMIN_TOKEN="$ADMIN" \
    -e SITE_NAME="${SITE_NAME:-SimpleURL}" \
    simpleurl:latest >/dev/null
  ok "容器已启动：$SERVICE_NAME"
  ADMIN_TOKEN_SHOWN="$ADMIN"
}

# ---------- Python 模式 ----------
install_python_mode() {
  info "使用 Python 模式部署（venv）"
  ensure_python
  PYVER=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')
  case "$PYVER" in 3.1[0-9]|3.[2-9][0-9]) : ;; *) die "需要 Python 3.10+，当前 $PYVER" ;; esac
  if ! python3 -m venv "$INSTALL_DIR/.venv" 2>/dev/null; then
    warn "创建虚拟环境失败，尝试补装 python3-venv…"
    install_venv_pkg || true
    python3 -m venv "$INSTALL_DIR/.venv" 2>/dev/null || die "创建虚拟环境失败，请手动安装 python3-venv 后重试"
  fi
  "$INSTALL_DIR/.venv/bin/pip" install -q --upgrade pip
  info "安装依赖…"
  "$INSTALL_DIR/.venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"

  ENV_FILE="$INSTALL_DIR/.env"
  if [ ! -f "$ENV_FILE" ]; then
    SECRET=$(gen_key 32); ADMIN=$(gen_key 16)
    cat > "$ENV_FILE" <<EOF
# SimpleURL 运行配置，由安装脚本生成
SITE_NAME=${SITE_NAME:-SimpleURL}
HOST=0.0.0.0
PORT=${PORT}
DATABASE_URL=sqlite:///$INSTALL_DIR/simpleurl.db
SECRET_KEY=$SECRET
ADMIN_TOKEN=$ADMIN
EOF
    chmod 600 "$ENV_FILE"
    ok "已生成 .env（含随机 SECRET_KEY 与 ADMIN_TOKEN）"
  else
    info "复用已有 .env"
  fi
  ADMIN_TOKEN_SHOWN=$(grep -E '^ADMIN_TOKEN=' "$ENV_FILE" | head -1 | cut -d= -f2-)

  START="$INSTALL_DIR/start.sh"
  cat > "$START" <<EOF
#!/usr/bin/env sh
set -e
cd "$INSTALL_DIR"
set -a; . ./.env; set +a
exec ./.venv/bin/python -m uvicorn app.main:app --host "\${HOST:-0.0.0.0}" --port "\${PORT:-8000}"
EOF
  chmod +x "$START"

  stop_running
  info "启动服务…"
  nohup "$START" > "$INSTALL_DIR/simpleurl.log" 2>&1 &
  echo $! > "$INSTALL_DIR/simpleurl.pid"

  # 装成 systemd 服务（有 root 权限时才做）
  if need_cmd systemctl && [ "$(id -u)" = "0" ]; then
    cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=SimpleURL short link service
After=network.target

[Service]
Type=simple
ExecStart=$START
Restart=always
RestartSec=3
WorkingDirectory=$INSTALL_DIR

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload >/dev/null 2>&1 || true
    ok "已注册 systemd 服务：systemctl status $SERVICE_NAME"
  fi
}

stop_running() {
  PID_FILE="$INSTALL_DIR/simpleurl.pid"
  if [ -f "$PID_FILE" ]; then
    OLD=$(cat "$PID_FILE" 2>/dev/null || true)
    if [ -n "${OLD:-}" ] && kill -0 "$OLD" 2>/dev/null; then
      info "停止旧进程 $OLD"
      kill "$OLD" 2>/dev/null || true
      sleep 1
    fi
    rm -f "$PID_FILE"
  fi
}

wait_health() {
  info "等待服务就绪…"
  i=0
  while [ $i -lt 40 ]; do
    if need_cmd curl && curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
      ok "健康检查通过"
      return 0
    fi
    i=$((i + 1)); sleep 1
  done
  warn "健康检查超时，最近的日志："
  tail -n 15 "$INSTALL_DIR/simpleurl.log" 2>/dev/null || true
  warn "完整日志：$INSTALL_DIR/simpleurl.log"
  return 1
}

summary() {
  IP=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
  [ -n "${IP:-}" ] || IP="127.0.0.1"
  printf "\n${GREEN}安装完成${RST}\n"
  printf "  首页      http://%s:%s\n" "$IP" "$PORT"
  printf "  管理后台  http://%s:%s/admin\n" "$IP" "$PORT"
  printf "  管理密钥  %s\n" "${ADMIN_TOKEN_SHOWN:-见 $INSTALL_DIR/.env}"
  printf "  安装目录  %s\n" "$INSTALL_DIR"
  printf "\n${DIM}数据存在 %s/simpleurl.db，备份这个文件就够了。${RST}\n" "$INSTALL_DIR"
  printf "${DIM}Powered by SimpleURL — https://github.com/techjiang/SimpleURL${RST}\n\n"
}

main() {
  banner
  fetch_source
  if have_docker; then
    install_docker_mode
    wait_health || true
  else
    warn "未检测到可用 Docker，使用 Python 直装"
    install_python_mode
    wait_health || true
  fi
  summary
}

main "$@"
