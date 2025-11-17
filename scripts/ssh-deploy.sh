#!/bin/bash
# SAG SSH 远程部署脚本
# 通过 SSH 在远程服务器上部署 SAG

set -e

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

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

# 显示使用说明
show_usage() {
    echo "Usage: $0 [user@]host [options]"
    echo ""
    echo "示例:"
    echo "  $0 root@192.168.1.100"
    echo "  $0 ubuntu@example.com"
    echo "  $0 root@192.168.1.100 --with-certs"
    echo ""
    echo "选项:"
    echo "  --with-certs    同时上传本地 certs/ 目录中的SSL证书"
    echo "  --repo URL      指定 Git 仓库地址（默认从当前仓库获取）"
    echo "  --branch NAME   指定分支名称（默认: main）"
    echo "  --dir PATH      指定服务器部署目录（默认: ~/sag）"
    echo "  --help          显示此帮助信息"
    echo ""
}

# 解析参数
SERVER=""
WITH_CERTS=false
REPO_URL=""
BRANCH="main"
DEPLOY_DIR="~/sag"

while [[ $# -gt 0 ]]; do
    case $1 in
        --with-certs)
            WITH_CERTS=true
            shift
            ;;
        --repo)
            REPO_URL="$2"
            shift 2
            ;;
        --branch)
            BRANCH="$2"
            shift 2
            ;;
        --dir)
            DEPLOY_DIR="$2"
            shift 2
            ;;
        --help)
            show_usage
            exit 0
            ;;
        -*)
            print_error "未知选项: $1"
            show_usage
            exit 1
            ;;
        *)
            if [ -z "$SERVER" ]; then
                SERVER="$1"
            else
                print_error "多余的参数: $1"
                show_usage
                exit 1
            fi
            shift
            ;;
    esac
done

# 检查必需参数
if [ -z "$SERVER" ]; then
    print_error "请指定服务器地址"
    show_usage
    exit 1
fi

# 获取 Git 仓库 URL（如果未指定）
if [ -z "$REPO_URL" ]; then
    if git remote get-url origin &> /dev/null; then
        REPO_URL=$(git remote get-url origin)
        print_info "使用当前仓库: $REPO_URL"
    else
        print_error "无法获取 Git 仓库 URL，请使用 --repo 指定"
        exit 1
    fi
fi

# 主流程
main() {
    echo ""
    echo -e "${BLUE}╔══════════════════════════════════════════╗${NC}"
    echo -e "${BLUE}║                                          ║${NC}"
    echo -e "${BLUE}║     SAG SSH 远程部署脚本 v1.0      ║${NC}"
    echo -e "${BLUE}║                                          ║${NC}"
    echo -e "${BLUE}╚══════════════════════════════════════════╝${NC}"
    echo ""

    print_section "连接信息"
    echo "服务器: $SERVER"
    echo "部署目录: $DEPLOY_DIR"
    echo "Git 仓库: $REPO_URL"
    echo "分支: $BRANCH"
    echo "上传证书: $([ "$WITH_CERTS" = true ] && echo "是" || echo "否")"
    echo ""

    # 测试SSH连接
    print_section "测试 SSH 连接"
    if ssh -o BatchMode=yes -o ConnectTimeout=5 "$SERVER" "echo '连接成功'" &> /dev/null; then
        print_success "SSH 连接正常"
    else
        print_error "SSH 连接失败"
        print_info "请检查："
        print_info "  1. 服务器地址是否正确"
        print_info "  2. SSH 密钥是否已配置"
        print_info "  3. 服务器是否在线"
        exit 1
    fi

    # 检查Docker
    print_section "检查服务器环境"
    print_info "检查 Docker..."
    if ssh "$SERVER" "command -v docker &> /dev/null"; then
        print_success "Docker 已安装"
    else
        print_error "Docker 未安装"
        print_info "请在服务器上安装 Docker，参考：docs/deploy/01-prerequisites.md"
        exit 1
    fi

    print_info "检查 Docker Compose..."
    if ssh "$SERVER" "docker compose version &> /dev/null"; then
        print_success "Docker Compose 已安装"
    else
        print_error "Docker Compose 未安装"
        print_info "请在服务器上安装 Docker Compose"
        exit 1
    fi

    # 克隆/更新代码
    print_section "部署代码"
    print_info "克隆/更新代码到 $DEPLOY_DIR..."

    ssh "$SERVER" bash << EOF
set -e

# 创建父目录
mkdir -p $(dirname $DEPLOY_DIR)

# 克隆或更新代码
if [ -d "$DEPLOY_DIR/.git" ]; then
    echo "代码目录已存在，执行更新..."
    cd $DEPLOY_DIR
    git fetch origin
    git checkout $BRANCH
    git pull origin $BRANCH
else
    echo "克隆代码..."
    git clone -b $BRANCH $REPO_URL $DEPLOY_DIR
fi

echo "代码部署完成"
EOF

    print_success "代码部署完成"

    # 上传证书（如果需要）
    if [ "$WITH_CERTS" = true ]; then
        print_section "上传 SSL 证书"

        if [ -f "certs/fullchain.pem" ] && [ -f "certs/privkey.pem" ]; then
            print_info "上传证书文件..."

            ssh "$SERVER" "mkdir -p $DEPLOY_DIR/certs"
            scp certs/fullchain.pem "$SERVER:$DEPLOY_DIR/certs/"
            scp certs/privkey.pem "$SERVER:$DEPLOY_DIR/certs/"

            print_success "证书上传完成"
        else
            print_warning "本地未找到证书文件（certs/fullchain.pem, certs/privkey.pem）"
            print_info "将使用 HTTP 模式部署"
        fi
    fi

    # 上传环境配置（交互式）
    print_section "环境配置"
    print_warning "需要配置 .env 文件"

    read -p "是否使用本地 .env 文件？(y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        if [ -f ".env" ]; then
            scp .env "$SERVER:$DEPLOY_DIR/"
            print_success "已上传本地 .env"
        else
            print_error "本地 .env 文件不存在"
            exit 1
        fi
    else
        print_info "请在服务器上手动编辑 .env："
        print_info "  ssh $SERVER"
        print_info "  cd $DEPLOY_DIR"
        print_info "  cp .env.example .env"
        print_info "  vim .env"
        read -p "配置完成后按任意键继续..." -n 1 -r
        echo
    fi

    # 执行部署
    print_section "启动服务"
    print_info "在服务器上执行部署脚本..."

    ssh "$SERVER" bash << EOF
set -e
cd $DEPLOY_DIR
bash scripts/deploy.sh
EOF

    print_success "部署完成！"

    # 显示访问信息
    print_section "访问信息"
    SERVER_IP=$(ssh "$SERVER" "hostname -I | awk '{print \$1}'")

    echo ""
    if [ "$WITH_CERTS" = true ]; then
        echo -e "${GREEN}🔒 HTTPS 访问${NC}"
        echo -e "   前端: ${BLUE}https://your-domain.com${NC}"
        echo -e "   API: ${BLUE}https://your-domain.com/docs${NC}"
    else
        echo -e "${GREEN}🌐 HTTP 访问${NC}"
        echo -e "   前端: ${BLUE}http://$SERVER_IP${NC}"
        echo -e "   API: ${BLUE}http://$SERVER_IP/docs${NC}"
    fi
    echo ""

    print_info "远程操作："
    echo "  连接服务器: ssh $SERVER"
    echo "  查看日志: ssh $SERVER \"cd $DEPLOY_DIR && docker compose logs -f\""
    echo "  查看状态: ssh $SERVER \"cd $DEPLOY_DIR && docker compose ps\""
    echo ""
}

# 执行主流程
main
