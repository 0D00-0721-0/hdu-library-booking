#!/bin/zsh -l

set -u

cd "$(dirname "$0")" || exit 1

if [[ -x ".venv/bin/python" ]]; then
  PYTHON_BIN=".venv/bin/python"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
else
  echo "没有找到 python 或 python3。"
  echo "请先安装 Python，或在终端中确认 python 命令可用。"
  read -r "?按回车关闭窗口..."
  exit 1
fi

if command -v caffeinate >/dev/null 2>&1; then
  CAFFEINATE_BIN="$(command -v caffeinate)"
else
  CAFFEINATE_BIN=""
fi

PORT="$("$PYTHON_BIN" - <<'PY'
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
    print(8765)
PY
)"

URL="http://127.0.0.1:${PORT}/"

echo "HDU 图书馆即时预约网页控制台"
echo "项目目录：$(pwd)"
echo "Python：$PYTHON_BIN"
echo "地址：$URL"
if [[ -n "$CAFFEINATE_BIN" ]]; then
  echo "防睡眠：已启用 caffeinate -is（不强制亮屏）"
else
  echo "防睡眠：未找到 caffeinate，按普通方式启动"
fi
echo ""
echo "浏览器会自动打开。关闭服务请按 Ctrl+C。"
echo ""

if [[ -n "$CAFFEINATE_BIN" ]]; then
  PYTHONDONTWRITEBYTECODE=1 "$CAFFEINATE_BIN" -is "$PYTHON_BIN" web_app.py --port "$PORT" --open
else
  PYTHONDONTWRITEBYTECODE=1 "$PYTHON_BIN" web_app.py --port "$PORT" --open
fi

echo ""
echo "服务已停止。"
read -r "?按回车关闭窗口..."
