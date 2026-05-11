#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/boji1334/boji1334-lite-faka.git}"
APP_DIR="${APP_DIR:-/opt/boji1334-lite-faka}"
APP_USER="${APP_USER:-litefaka}"
SERVICE_NAME="${SERVICE_NAME:-boji1334-lite-faka}"
ENV_FILE="${ENV_FILE:-/etc/${SERVICE_NAME}.env}"
APP_PORT="${APP_PORT:-18080}"
APP_HOST="${APP_HOST:-0.0.0.0}"
MEMORY_MAX="${MEMORY_MAX:-128M}"
CPU_QUOTA="${CPU_QUOTA:-30%}"
RESERVATION_TTL_MINUTES="${RESERVATION_TTL_MINUTES:-120}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run as root, for example: sudo bash deploy/install.sh"
  exit 1
fi

need_cmd() {
  command -v "$1" >/dev/null 2>&1
}

prompt_default() {
  local var_name="$1"
  local label="$2"
  local default_value="$3"
  local secret="${4:-}"
  local current_value="${!var_name:-}"
  if [ -n "$current_value" ]; then
    return
  fi
  local input=""
  if [ -t 0 ]; then
    if [ "$secret" = "secret" ]; then
      read -r -s -p "$label [$default_value]: " input
      echo
    else
      read -r -p "$label [$default_value]: " input
    fi
  fi
  printf -v "$var_name" "%s" "${input:-$default_value}"
}

random_secret() {
  if need_cmd openssl; then
    openssl rand -hex 32
  else
    python3 - <<'PY'
import secrets
print(secrets.token_hex(32))
PY
  fi
}

prompt_default BASE_URL "Public site URL" "http://YOUR_DOMAIN:${APP_PORT}"
prompt_default SITE_NAME "Site name" "轻量发卡网"
prompt_default ADMIN_USER "Admin username" "admin"
prompt_default ADMIN_PASS "Admin password" "$(random_secret)" "secret"
SECRET_KEY="${SECRET_KEY:-$(random_secret)}"

if ss -ltn "( sport = :${APP_PORT} )" 2>/dev/null | grep -q ":${APP_PORT}"; then
  if ! systemctl is-active --quiet "${SERVICE_NAME}" 2>/dev/null; then
    echo "Port ${APP_PORT} is already in use. Choose another port with APP_PORT=18081."
    exit 1
  fi
fi

if ! need_cmd python3 || ! need_cmd git; then
  if need_cmd apt-get; then
    apt-get update
    apt-get install -y python3 git ca-certificates
  else
    echo "python3 and git are required. Please install them first."
    exit 1
  fi
fi

if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi

mkdir -p "$APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch --depth=1 origin main
  git -C "$APP_DIR" reset --hard origin/main
else
  git clone --depth=1 "$REPO_URL" "$APP_DIR"
fi

mkdir -p "$APP_DIR/data"
chown -R "$APP_USER:$APP_USER" "$APP_DIR/data"
chmod 700 "$APP_DIR/data"

cat >"$ENV_FILE" <<EOF
APP_DIR=${APP_DIR}
DATA_DIR=${APP_DIR}/data
HOST=${APP_HOST}
PORT=${APP_PORT}
BASE_URL=${BASE_URL}
SITE_NAME=${SITE_NAME}
ADMIN_USER=${ADMIN_USER}
ADMIN_PASS=${ADMIN_PASS}
SECRET_KEY=${SECRET_KEY}
RESERVATION_TTL_MINUTES=${RESERVATION_TTL_MINUTES}
EOF
chmod 600 "$ENV_FILE"

cat >"/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=Boji1334 Lite Faka
After=network.target

[Service]
Type=simple
User=${APP_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=/usr/bin/python3 ${APP_DIR}/app.py
Restart=always
RestartSec=2
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=${APP_DIR}/data
MemoryMax=${MEMORY_MAX}
CPUQuota=${CPU_QUOTA}

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

echo
echo "Installed ${SERVICE_NAME}"
echo "URL: ${BASE_URL}"
echo "Admin: ${BASE_URL}/admin"
echo "Admin username: ${ADMIN_USER}"
echo "Admin password: ${ADMIN_PASS}"
echo "Service: systemctl status ${SERVICE_NAME} --no-pager"
echo "Logs: journalctl -u ${SERVICE_NAME} -f"
echo
echo "This installer does not change nginx, Docker, sub2api, or firewall rules."
