#!/bin/bash
# SAG 部署脚本 - 本地/服务器部署
# 自动检测 SSL 证书并启用 HTTPS

set -e  # 遇到错误立即退出

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 辅助函数
print_info() {
    echo -e "${BLUE}ℹ ${NC}$1"
}

print_success() {
    echo -e "${GREEN}✓${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}⚠${NC} $1"
}

print_error() {
    echo -e "${RED}✗${NC} $1"
}

print_section() {
    echo ""
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BLUE}  $1${NC}"
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
}

# 检查依赖
check_dependencies() {
    print_section "检查系统依赖"

    # 检查 Docker
    if ! command -v docker &> /dev/null; then
        print_error "Docker 未安装"
        print_info "请参考文档安装：docs/deploy/01-prerequisites.md"
        exit 1
    fi
    print_success "Docker 已安装: $(docker --version)"

    # 检查 Docker Compose
    if ! docker compose version &> /dev/null; then
        print_error "Docker Compose 未安装"
        print_info "请参考文档安装：docs/deploy/01-prerequisites.md"
        exit 1
    fi
    print_success "Docker Compose 已安装: $(docker compose version --short)"
}

# 检查环境配置
check_env() {
    print_section "检查环境配置"

    if [ ! -f .env ]; then
        print_warning ".env 文件不存在，从模板创建..."
        if [ -f .env.example ]; then
            cp .env.example .env
            print_success "已从 .env.example 创建 .env"
            print_warning "请编辑 .env 文件，配置必需参数（特别是 LLM_API_KEY）"
            print_info "使用命令：vim .env 或 nano .env"
            exit 0
        else
            print_error ".env.example 文件不存在"
            exit 1
        fi
    fi

    # 检查关键配置
    if ! grep -q "LLM_API_KEY=" .env || grep -q "LLM_API_KEY=$" .env; then
        print_warning "LLM_API_KEY 未配置"
        print_info "请编辑 .env 文件设置 LLM_API_KEY"
        read -p "是否继续部署？(y/N) " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            exit 0
        fi
    fi

    print_success ".env 配置已就绪"
}

# 检测 SSL 证书
check_ssl_cert() {
    print_section "检测 SSL 证书"

    if [ -f "certs/fullchain.pem" ] && [ -f "certs/privkey.pem" ]; then
        print_success "检测到 SSL 证书"

        # 验证证书有效期
        expiry=$(openssl x509 -in certs/fullchain.pem -noout -enddate | cut -d= -f2)
        print_info "证书有效期至: $expiry"

        # 检查证书是否过期
        if ! openssl x509 -in certs/fullchain.pem -noout -checkend 0 &> /dev/null; then
            print_error "SSL 证书已过期"
            print_info "请更新证书或删除过期证书使用 HTTP"
            exit 1
        fi

        print_info "将使用 HTTPS 模式部署"
        print_info "HTTP (80端口) 将重定向到 HTTPS (443端口)"
        COMPOSE_FILE="docker-compose.https.yml"
        SSL_ENABLED=true
    else
        print_warning "未检测到 SSL 证书"
        print_info "将使用 HTTP 模式部署（80端口）"
        print_info "如需 HTTPS，请参考：docs/deploy/03-ssl-setup.md"
        COMPOSE_FILE="docker-compose.yml"
        SSL_ENABLED=false
    fi
}

# 拉取/构建镜像
build_images() {
    print_section "构建 Docker 镜像"

    print_info "使用配置文件: $COMPOSE_FILE"
    print_info "开始构建镜像（首次需要5-10分钟）..."

    if docker compose -f "$COMPOSE_FILE" build; then
        print_success "镜像构建完成"
    else
        print_error "镜像构建失败"
        print_info "请检查日志并参考文档：docs/deploy/02-quick-deploy.md"
        exit 1
    fi
}

# 启动服务
start_services() {
    print_section "启动服务"

    print_info "启动所有服务..."

    if docker compose -f "$COMPOSE_FILE" up -d; then
        print_success "服务启动成功"
    else
        print_error "服务启动失败"
        print_info "查看日志：docker compose -f $COMPOSE_FILE logs -f"
        exit 1
    fi
}

# 等待服务就绪
wait_for_services() {
    print_section "等待服务就绪"

    print_info "等待数据库初始化（最多60秒）..."

    timeout=60
    elapsed=0

    while [ $elapsed -lt $timeout ]; do
        if docker compose -f "$COMPOSE_FILE" ps mysql | grep -q "healthy"; then
            print_success "MySQL 已就绪"
            break
        fi
        sleep 2
        elapsed=$((elapsed + 2))
        echo -n "."
    done
    echo ""

    if [ $elapsed -ge $timeout ]; then
        print_warning "MySQL 启动超时，但将继续部署"
    fi

    print_info "等待 ElasticSearch 就绪（最多60秒）..."

    elapsed=0
    while [ $elapsed -lt $timeout ]; do
        if docker compose -f "$COMPOSE_FILE" ps elasticsearch | grep -q "healthy"; then
            print_success "ElasticSearch 已就绪"
            break
        fi
        sleep 2
        elapsed=$((elapsed + 2))
        echo -n "."
    done
    echo ""

    if [ $elapsed -ge $timeout ]; then
        print_warning "ElasticSearch 启动超时，但将继续部署"
    fi
}

# 验证部署
verify_deployment() {
    print_section "验证部署"

    # 检查所有服务状态
    print_info "检查服务状态..."
    docker compose -f "$COMPOSE_FILE" ps

    # 测试健康检查端点
    print_info "测试 API 健康检查..."
    sleep 5  # 等待 Nginx 启动

    if [ "$SSL_ENABLED" = true ]; then
        if curl -sfk https://localhost/health > /dev/null 2>&1; then
            print_success "API 健康检查通过"
        else
            print_warning "API 健康检查失败（可能还在启动中）"
            print_info "稍后可手动验证：curl -k https://localhost/health"
        fi
    else
        if curl -sf http://localhost/health > /dev/null 2>&1; then
            print_success "API 健康检查通过"
        else
            print_warning "API 健康检查失败（可能还在启动中）"
            print_info "稍后可手动验证：curl http://localhost/health"
        fi
    fi
}

# 显示访问信息
show_access_info() {
    print_section "部署完成"

    # 获取服务器IP
    SERVER_IP=$(hostname -I | awk '{print $1}')

    echo ""
    print_success "SAG 已成功部署！"
    echo ""

    if [ "$SSL_ENABLED" = true ]; then
        echo -e "${GREEN}🔒 HTTPS 访问${NC}"
        echo -e "   前端: ${BLUE}https://your-domain.com${NC}"
        echo -e "   API文档: ${BLUE}https://your-domain.com/docs${NC}"
        echo ""
        echo -e "${YELLOW}HTTP 访问将自动重定向到 HTTPS${NC}"
    else
        echo -e "${GREEN}🌐 HTTP 访问${NC}"
        echo -e "   前端: ${BLUE}http://$SERVER_IP${NC}"
        echo -e "   API文档: ${BLUE}http://$SERVER_IP/docs${NC}"
        echo ""
        echo -e "${YELLOW}如需 HTTPS，请参考：docs/deploy/03-ssl-setup.md${NC}"
    fi

    echo ""
    echo -e "${BLUE}常用命令${NC}"
    echo -e "   查看日志: ${GREEN}docker compose -f $COMPOSE_FILE logs -f${NC}"
    echo -e "   查看状态: ${GREEN}docker compose -f $COMPOSE_FILE ps${NC}"
    echo -e "   停止服务: ${GREEN}docker compose -f $COMPOSE_FILE stop${NC}"
    echo -e "   重启服务: ${GREEN}docker compose -f $COMPOSE_FILE restart${NC}"
    echo ""

    print_info "完整文档：docs/deploy/README.md"
}

# 主流程
main() {
    echo ""
    echo -e "${BLUE}╔══════════════════════════════════════════╗${NC}"
    echo -e "${BLUE}║                                          ║${NC}"
    echo -e "${BLUE}║       SAG 部署脚本 v1.0            ║${NC}"
    echo -e "${BLUE}║                                          ║${NC}"
    echo -e "${BLUE}╚══════════════════════════════════════════╝${NC}"
    echo ""

    check_dependencies
    check_env
    check_ssl_cert
    build_images
    start_services
    wait_for_services
    verify_deployment
    show_access_info
}

# 执行主流程
main
