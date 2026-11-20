#!/bin/sh
# 视频号逐字稿 - 环境准备（macOS，双击运行）
cd "$(dirname "$0")" || exit 1

echo ""
echo "============================================================"
echo "  视频号逐字稿 - 环境准备"
echo "============================================================"
echo ""

PY=""
WB="$HOME/.workbuddy/binaries/python/envs/default/bin/python"
if [ -x "$WB" ]; then
  PY="$WB"
elif command -v python3 >/dev/null 2>&1; then
  PY="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  PY="$(command -v python)"
fi

if [ -z "$PY" ]; then
  echo "[错误] 找不到 Python。"
  echo ""
  echo "  请任选其一："
  echo "    1. 安装 WorkBuddy 桌面版（自带 Python 运行环境）"
  echo "    2. 从 https://www.python.org 安装 Python 3.9 或更高版本"
  echo ""
  printf "按回车退出…"
  read -r _
  exit 1
fi

echo "使用 Python：$PY"
echo ""

"$PY" scripts/sph2txt.py --setup
RC=$?

echo ""
if [ "$RC" -ne 0 ]; then
  echo "环境准备未完成（错误码 $RC）。请把上面的提示截图反馈。"
else
  echo "准备完成。若提示还差扫码登录，请双击「首次登录.command」。"
fi
echo ""
printf "按回车关闭窗口…"
read -r _
exit "$RC"
