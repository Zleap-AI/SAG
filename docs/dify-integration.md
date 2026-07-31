# 将 SAG 作为 Dify 外部知识库

Dify 只在应用运行时向 SAG 请求检索证据；上传、解析、分块和 Embedding 仍在 SAG 中完成。

~~~text
Dify API → SSRF Proxy → Docker 网络中的 sag:8000 → SAG /api/v1/dify/retrieval
~~~

一个 Dify 外部知识库对应一个 SAG 信源（source.id）。

## 1. 配置 SAG

在 SAG 根目录的 .env 中设置：

~~~dotenv
# Dify 专用 API Key。Dify 中必须填写相同的值。
SAG_DIFY_API_KEY=替换成强随机密钥

# Dify 专用检索策略：
# vector（默认）：低延迟，不进行 SAG 内部 LLM 精排。
# multi：实体扩展 + SAG 内部 LLM 精排，延迟更高。
SAG_DIFY_SEARCH_STRATEGY=vector
~~~

此策略只影响 Dify 外部知识库接口，不影响 SAG Web 的检索策略。

### 验证：配置进入 API 容器

~~~bash
docker exec sag-api-1 sh -lc 'python - <<'"'"'PY'"'"'
import os
print("dify_key_configured=", bool(os.getenv("SAG_DIFY_API_KEY")))
print("dify_search_strategy=", os.getenv("SAG_DIFY_SEARCH_STRATEGY"))
PY'
~~~

正确结果：

~~~text
dify_key_configured= True
dify_search_strategy= vector
~~~

## 2. 将 SAG API 加入 Dify Docker 网络

查看 Dify 网络名：

~~~bash
docker network ls
~~~

默认 Dify Compose 项目名为 docker 时，网络通常是 docker_default。在 SAG 根目录执行：

~~~bash
DIFY_NETWORK_NAME=docker_default \
docker compose -f compose.yaml -f compose.dify.yaml up -d --build --force-recreate --no-deps api
~~~

此命令只重建 SAG API，不会停止 SAG Web、Dify 或数据库。

### 验证：网络与健康状态

~~~bash
docker inspect sag-api-1 \
  --format '{{range $name, $network := .NetworkSettings.Networks}}{{println $name}}{{end}}'

docker run --rm --network docker_default busybox:latest \
  wget -S -O - -T 10 http://sag:8000/api/v1/system/ready
~~~

网络输出应包含 sag_default 和 docker_default；健康接口应返回 HTTP 200 与：

~~~json
{"status":"ready","db":true}
~~~

sag:8000 是 Docker 内部地址。宿主机端口以 docker ps 显示为准。

## 3. 配置 Dify SSRF

编辑 Dify docker/.env：

~~~dotenv
SSRF_PROXY_ALLOW_PRIVATE_DOMAINS=sag
SSRF_DEFAULT_TIME_OUT=30
SSRF_DEFAULT_READ_TIME_OUT=30
~~~

- SSRF_PROXY_ALLOW_PRIVATE_DOMAINS：允许 Dify 访问 Docker 私网中的 sag。
- SSRF_DEFAULT_TIME_OUT：整个请求最长时间（秒）。
- SSRF_DEFAULT_READ_TIME_OUT：连接后等待响应的最长时间（秒）。

使用默认 vector 时，30 秒通常足够；使用 multi 时，应根据 SAG 单信源超时和 LLM 响应时间调高两个值。

在 Dify docker 目录重建：

~~~bash
docker compose up -d --force-recreate api ssrf_proxy
~~~

### 验证：Dify 服务拿到配置

~~~bash
docker exec docker-ssrf_proxy-1 sh -lc \
  'env | grep "^SSRF_PROXY_ALLOW_PRIVATE_DOMAINS="'

docker exec docker-api-1 sh -lc \
  'env | grep -E "^(SSRF_DEFAULT_TIME_OUT|SSRF_DEFAULT_READ_TIME_OUT)="'
~~~

应看到：

~~~text
SSRF_PROXY_ALLOW_PRIVATE_DOMAINS=sag
SSRF_DEFAULT_TIME_OUT=30
SSRF_DEFAULT_READ_TIME_OUT=30
~~~

## 4. 在 Dify 中绑定信源

1. 在 SAG Web 中准备已处理完成的信源，复制完整 source.id。
2. 在 Dify 进入 **知识库 → 连接外部知识库**，创建外部知识 API：
   - Endpoint：http://sag:8000/api/v1/dify
   - API Key：SAG_DIFY_API_KEY 的完整值
3. 创建外部知识库时，将 Knowledge ID 填为完整 source.id。

不要在 Endpoint 后填写 /retrieval，Dify 会自动追加；不要使用 localhost 或 HTTPS。

### 验证：接口鉴权

在 SAG 根目录执行：

~~~bash
KEY=$(sed -n 's/^SAG_DIFY_API_KEY=//p' .env)

curl -i -sS -X POST \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  http://127.0.0.1:8001/api/v1/dify/retrieval \
  -d '{"knowledge_id":"","query":""}'
~~~

应返回 HTTP 200 和：

~~~json
{"records":[]}
~~~

将 8001 替换为 docker ps 中 SAG API 的宿主机端口。若 Dify 页面显示“SSRF protection”，但 ssrf_proxy 日志中有 HIER_DIRECT/... TCP_MISS/403，请优先检查 Dify 保存的 API Key 是否与 SAG .env 中的密钥一致。

## 5. 验证策略

在 Dify 外部知识库页面执行命中测试。响应记录的 metadata 会包含：

~~~json
{
  "retrieval_strategy": "vector",
  "fallback_used": false
}
~~~

- `retrieval_strategy` 是本次实际使用的策略；multi 请求在 SAG 内部因超时/失败/空结果回退到 vector 时，会显示为 `vector`。
- `fallback_used` 为 `true` 表示 multi 已回退到 vector。

## 策略选择

| 策略 | SAG 内部 LLM | 延迟 | 场景 |
| --- | --- | --- | --- |
| vector（默认） | 不进行 LLM 精排 | 低 | Dify 常规低延迟问答 |
| multi | 实体提取和候选精排使用 SAG 配置的 LLM | 高 | 可接受更长等待、追求更丰富召回 |

Dify 应用配置的 LLM 仍负责最终自然语言回答；此配置只决定 SAG 返回的检索证据。

