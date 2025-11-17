#!/bin/bash

# SAG 全栈启动脚本
# 启动所有服务：MySQL, Elasticsearch, Redis, API, Web

set -e

echo "🚀 SAG 全栈启动..."
echo ""

# 检查 Docker
if ! command -v docker &> /dev/null; then
    echo "❌ Docker 未安装，请先安装 Docker"
    exit 1
fi

# 检查 docker-compose
if ! command -v docker-compose &> /dev/null; then
    echo "❌ docker-compose 未安装，请先安装 docker-compose"
    exit 1
fi

# 检查环境变量
if [ ! -f .env ]; then
    echo "⚠️  .env 文件不存在，从模板创建..."
    cp .env.example .env
    echo "📝 请编辑 .env 文件配置 LLM_API_KEY"
    echo ""
fi

# 启动所有服务
echo "📦 启动所有服务..."
docker-compose up -d

echo ""
echo "⏳ 等待服务就绪..."
sleep 10

# 检查服务状态
echo ""
echo "🔍 检查服务状态..."
docker-compose ps

echo ""
echo "✅ SAG 启动完成！"
echo ""
echo "🌐 访问地址:"
echo "   - Web UI:    http://localhost:3000"
echo "   - API:       http://localhost:8000"
echo "   - API Docs:  http://localhost:8000/api/docs"
echo ""
echo "📊 服务状态:"
echo "   - MySQL:         localhost:3306"
echo "   - Elasticsearch: localhost:9200"
echo "   - Redis:         localhost:6379"
echo ""
echo "🛠️  管理命令:"
echo "   查看日志:  docker-compose logs -f"
echo "   停止服务:  docker-compose down"
echo "   重启服务:  docker-compose restart"
echo ""

