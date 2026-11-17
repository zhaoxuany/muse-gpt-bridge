@echo off
REM Windows 真实调用测试包 v2 一键启动.
REM 用法: 把 windows-siwc-test-v2 文件夹拷到你电脑上, 双击本文件.
REM 它会: 装依赖 -> 跑授权 (第一次) -> 跑三轮真实调用测试.
REM 也可以在 PowerShell 里手动跑: python siwc_auth.py / python test_siwc.py
chcp 65001 >nul
cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
  echo [错误] 找不到 python, 请先装 Python 3.10 以上 (装时勾上 Add python.exe to PATH).
  echo 下载地址: https://www.python.org/downloads/
  pause
  exit /b 1
)

echo 正在装依赖 (只需要装一次)...
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo [错误] 依赖装不上, 先停下来, 把报错截图发给 Muse, 不要反复试.
  pause
  exit /b 1
)

if not exist "%USERPROFILE%\.muse-gpt-bridge\siwc_tokens.json" (
  echo.
  echo 还没授权过, 先跑授权助手 (浏览器里登录 ChatGPT、选 workspace、勾选允许使用计划用量).
  python siwc_auth.py
  if errorlevel 1 (
    echo [错误] 授权没完成, 先停下来, 把报错发给 Muse.
    pause
    exit /b 1
  )
) else (
  echo 已检测到本地 token 文件, 跳过授权, 直接跑测试.
  echo 如果 token 过期了 (超过 30 天没用), 测试会提示, 再跑一次 python siwc_auth.py.
)

echo.
echo 正在跑三轮真实调用测试 (耳机+宿舍用品, 第 3 轮会让 GPT 追问一个需你决策的问题)...
python test_siwc.py %*
if errorlevel 1 (
  echo.
  echo [提示] 测试没跑完, 把报错原文和 siwc_test_result.json 一起发给 Muse, 不要自己反复试.
  pause
  exit /b 1
)

echo.
echo 全部完成. 结果在 siwc_test_result.json, 直接把这个文件发给 Muse 就行 (里面没有 token).
pause
