@echo off
echo Starting SAG Web for Windows...

REM 检查 Node.js 版本
node --version
if %errorlevel% neq 0 (
    echo Error: Node.js is not installed or not in PATH
    echo Please install Node.js 18+ from https://nodejs.org/
    pause
    exit /b 1
)

REM 清理缓存
echo Cleaning cache...
if exist node_modules rmdir /s /q node_modules
if exist .next rmdir /s /q .next
if exist package-lock.json del package-lock.json

REM 重新安装依赖
echo Installing dependencies...
npm install

REM 创建环境变量文件（如果不存在）
if not exist .env.local (
    echo Creating .env.local file...
    echo NEXT_PUBLIC_API_URL=http://localhost:8000 > .env.local
)

REM 启动开发服务器
echo Starting development server...
npm run dev
