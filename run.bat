@echo off
REM nanobee Windows 快捷启动脚本
REM 自动创建虚拟环境、安装依赖、启动对话

setlocal enabledelayedexpansion

echo ========================================
echo   nanobee 🐝 - AI Agent 微内核框架
echo ========================================
echo.

REM 检查 Python 是否安装
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未找到 Python，请先安装 Python >= 3.11
    echo 下载地址: https://www.python.org/downloads/
    pause
    exit /b 1
)

echo [1/4] 检查 Python 版本...
python -c "import sys; exit(0 if sys.version_info >= (3, 11) else 1)" 2>nul
if %errorlevel% neq 0 (
    echo [错误] Python 版本低于 3.11，请升级 Python
    pause
    exit /b 1
)
python --version
echo.

REM 创建虚拟环境（如果不存在）
if not exist ".venv" (
    echo [2/4] 创建虚拟环境...
    python -m venv .venv
    if %errorlevel% neq 0 (
        echo [错误] 虚拟环境创建失败
        pause
        exit /b 1
    )
) else (
    echo [2/4] 虚拟环境已存在，跳过
)
echo.

REM 激活虚拟环境并安装依赖
echo [3/4] 安装依赖...
call .venv\Scripts\activate.bat
pip install -e ".[dev]" --quiet
if %errorlevel% neq 0 (
    echo [错误] 依赖安装失败
    pause
    exit /b 1
)
echo.

REM 检查配置文件
if not exist "nanobee.yaml" (
    echo [4/4] 复制配置文件...
    if exist "nanobee.yaml.example" (
        copy nanobee.yaml.example nanobee.yaml >nul
        echo 已创建 nanobee.yaml，请编辑并填入 API Key
        echo.
    ) else (
        echo [警告] 未找到 nanobee.yaml.example
        echo.
    )
)

echo [4/4] 启动 nanobee...
echo.
echo ========================================
echo   按 Ctrl+C 退出对话
echo ========================================
echo.

REM 启动对话
nanobee run

pause
