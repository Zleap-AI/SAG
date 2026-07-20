.DEFAULT_GOAL := help
.PHONY: help dev api web install install-api install-web build compose-config compose-up compose-ps compose-logs compose-down compose-up-postgres compose-down-postgres

help: ## 显示可用命令
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: install-api install-web ## 安装前后端依赖

install-api: ## 安装后端依赖（editable + dev）
	cd apps/api && python -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"

install-web: ## 安装前端依赖
	cd apps/web && npm ci

dev: ## 提示：分别在两个终端运行 make api / make web
	@echo "在一个终端运行:  make api"
	@echo "在另一个终端运行: make web"

api: ## 启动后端（本地零依赖 SQLite+LanceDB，热重载，局域网可访问）
	cd apps/api && . .venv/bin/activate && uvicorn sag_api.main:app --reload --host 0.0.0.0 --port 8000

web: ## 启动前端（Next dev，局域网可访问）
	cd apps/web && npm run dev -- -H 0.0.0.0

build: ## 构建前端产物
	cd apps/web && npm run build

compose-config: ## 校验默认 Docker 配置（SQLite + LanceDB）
	docker compose config --quiet

compose-up: ## Docker 快速启动（web + api，数据持久化）
	docker compose up -d --build

compose-ps: ## 查看 Docker 服务状态
	docker compose ps

compose-logs: ## 持续查看 Docker 日志
	docker compose logs -f api web

compose-down: ## 停止 compose
	docker compose down

compose-up-postgres: ## 使用 Postgres + pgvector 覆盖启动（需先配置 .env）
	docker compose -f compose.yaml -f compose.postgres.yaml up -d --build

compose-down-postgres: ## 停止 Postgres 覆盖部署
	docker compose -f compose.yaml -f compose.postgres.yaml down
