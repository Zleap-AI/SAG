#!/bin/bash

# SAG 开发环境启动脚本
# 只启动基础服务（MySQL, ES, Redis），前后端在本地运行

set -e

echo "🔧 SAG 开发环境启动..."
echo ""

# 检查并初始化 NLTK 数据
echo "🔍 检查 NLTK 数据..."
if python scripts/init_nltk.py; then
    echo "✅ NLTK 数据就绪"
else
    echo "❌ NLTK 数据初始化失败，请检查日志"
    exit 1
fi

echo ""

# 启动基础服务
echo "📦 启动基础服务（MySQL, Elasticsearch, Redis）..."
docker-compose -f docker-compose.dev.yml up -d

echo ""
echo "⏳ 等待服务就绪..."
sleep 10

echo ""
echo "✅ 基础服务启动完成！"
echo ""
echo "📊 服务状态:"
echo "   - MySQL:         localhost:3306"
echo "   - Elasticsearch: localhost:9200"
echo "   - Redis:         localhost:6379"
echo ""
echo "🚀 接下来请手动启动:"
echo ""
echo "   # 终端 1: 启动后端 API"
echo "   python -m sag.api.main"
echo ""
echo "   # 终端 2: 启动前端"
echo "   cd web && npm run dev"
echo ""
echo "🌐 访问地址:"
echo "   - Web UI:    http://localhost:3000"
echo "   - API:       http://localhost:8000"
echo "   - API Docs:  http://localhost:8000/api/docs"
echo ""

