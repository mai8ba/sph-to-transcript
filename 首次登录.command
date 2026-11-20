#!/bin/sh
# 视频号逐字稿 - 微信扫码登录（macOS，双击运行）
cd "$(dirname "$0")" || exit 1

echo ""
echo "============================================================"
echo "  视频号逐字稿 - 微信扫码登录"
echo "============================================================"
echo ""
echo "接下来会弹出一个浏览器窗口，请用手机微信扫码并确认登录。"
echo "登录状态只需保存一次，之后不用再扫。"
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
  echo "[错误] 找不到 Python，请先双击「一键准备.command」。"
  echo ""
  printf "按回车退出…"
  read -r _
  exit 1
fi

"$PY" scripts/sph2txt.py --login
RC=$?

echo ""
if [ "$RC" -eq 0 ]; then
  echo "登录成功。现在可以直接把视频号链接发给助手出稿了。"
else
  echo "登录未完成。可以再双击本文件重试一次。"
fi
echo ""
printf "按回车关闭窗口…"
read -r _
exit "$RC"
