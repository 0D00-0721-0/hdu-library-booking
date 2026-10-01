#!/bin/zsh -l

set -euo pipefail

cd "$(dirname "$0")"

if command -v python >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python)"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3)"
else
  echo "没有找到 Python。"
  read -r "?按回车关闭窗口..."
  exit 1
fi

if ! command -v cloudflared >/dev/null 2>&1; then
  echo "没有找到 cloudflared，请先运行：brew install cloudflared"
  read -r "?按回车关闭窗口..."
  exit 1
fi

PASSWORD_FILE=".cloudflare-access-password"
if [[ ! -f "$PASSWORD_FILE" ]]; then
  umask 077
  openssl rand -base64 24 > "$PASSWORD_FILE"
fi

export HDU_WEB_USERNAME="hdu"
export HDU_WEB_PASSWORD="$(< "$PASSWORD_FILE")"

PORT="$($PYTHON_BIN - <<'PY'
import socket

for port in range(8765, 8785):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            continue
        print(port)
        break
else:
    raise SystemExit("没有可用端口")
PY
)"

APP_PID=""
cleanup() {
  if [[ -n "$APP_PID" ]]; then
    kill "$APP_PID" 2>/dev/null || true
    wait "$APP_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

PYTHONDONTWRITEBYTECODE=1 "$PYTHON_BIN" web_app.py --host 127.0.0.1 --port "$PORT" &
APP_PID=$!

echo ""
echo "HDU 图书馆预约控制台（Cloudflare 临时通道）"
echo "用户名：$HDU_WEB_USERNAME"
echo "密码：$HDU_WEB_PASSWORD"
echo ""
echo "稍后请在下面找到 https://……trycloudflare.com 地址。"
echo "Mac 需要保持开机，本窗口需要保持运行；按 Ctrl+C 停止。"
echo ""

cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:$PORT"
