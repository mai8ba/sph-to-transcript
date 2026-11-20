@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo ============================================================
echo   视频号逐字稿 - 环境准备
echo ============================================================
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
  echo [错误] 找不到 Python。
  echo.
  echo   请任选其一：
  echo     1. 安装 WorkBuddy 桌面版（自带 Python 运行环境）
  echo     2. 自行安装 Python 3.9 或更高版本，安装时勾选 "Add to PATH"
  echo.
  pause
  exit /b 1
)

echo 使用 Python：%PY%
echo.

"%PY%" "scripts\sph2txt.py" --setup
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
  echo 环境准备未完成，错误码 %RC%。请把上面的提示截图反馈。
) else (
  echo 准备完成。若提示还差扫码登录，请双击「首次登录.bat」。
)
echo.
pause
exit /b %RC%
