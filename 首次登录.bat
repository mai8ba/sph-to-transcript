@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo.
echo ============================================================
echo   视频号逐字稿 - 微信扫码登录
echo ============================================================
echo.
echo 接下来会弹出一个浏览器窗口，请用手机微信扫码并确认登录。
echo 登录状态只需保存一次，之后不用再扫。
echo.

set "PY="
set "WB=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if exist "%WB%" set "PY=%WB%"

if not defined PY (
  for %%C in (python.exe python3.exe) do (
    if not defined PY (
      for /f "delims=" %%P in ('where %%C 2^>nul') do (
        if not defined PY set "PY=%%P"
      )
    )
  )
)

if not defined PY (
  echo [错误] 找不到 Python，请先双击「一键准备.bat」。
  echo.
  pause
  exit /b 1
)

"%PY%" "scripts\sph2txt.py" --login
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
  echo 登录成功。现在可以直接把视频号链接发给助手出稿了。
) else (
  echo 登录未完成。可以再双击本文件重试一次。
)
echo.
pause
exit /b %RC%
