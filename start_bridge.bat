@echo off
chcp 65001 >nul
cd /d "%~dp0"
if "%BRIDGE_TOKEN%"=="" (
 echo [拒绝启动] 当前进程未提供临时 BRIDGE_TOKEN。
 exit /b 1
)
if "%SIWC_MODEL%"=="" (
 echo [拒绝启动] 请由操作助手明确选择账号实际可用模型。
 exit /b 1
)
if "%MUSE_GPT_ADAPTER%"=="" set "MUSE_GPT_ADAPTER=siwc"
set "PY=python"
if exist "venv\Scripts\python.exe" set "PY=venv\Scripts\python.exe"
%PY% -X utf8 -m bridge.server
