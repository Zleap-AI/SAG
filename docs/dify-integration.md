# 将 SAG 作为 Dify 外部知识库

SAG 可以实现 Dify 的外部知识库检索协议。文档上传、解析、分块、Embedding
和 event–entity 抽取仍由 SAG 完成；Dify 只在应用运行时向 SAG 请求检索证据。
该能力完全可选，不会替换或修改 Dify、SAG 的其他知识库功能。

## 前提

- SAG 与 Dify 均通过 Docker Compose 运行。
- SAG 信源中的文档已经处理完成。
- Dify 容器可以通过一个共同的 Docker 网络访问 SAG API。
- 当前集成按 SAG 的单用户边界设计，一个 Dify 外部知识库绑定一个 SAG 信源。

## 1. 初始化 SAG 的 Dify 密钥

在 SAG 仓库根目录执行：

```bash
make setup-dify
```

命令会生成强随机密钥并保存到被 Git 忽略的根目录 `.env`，同时输出：

```text
Endpoint: http://sag:8000/api/v1/dify
API Key: <生成的密钥>
```

已有非空 `SAG_DIFY_API_KEY` 时不会覆盖。未执行该命令且未手工设置密钥时，
Dify 兼容端点返回 503，SAG 的默认启动和其他功能不受影响。

## 2. 将 SAG API 加入 Dify 网络

先在 Dify 的 `docker` 目录确认网络名：

```bash
docker compose ps
docker network ls
```

按默认目录名启动的 Dify 通常使用 `docker_default`。在 SAG 根目录叠加可选
Compose 文件启动：

```bash
DIFY_NETWORK_NAME=docker_default \
docker compose -f compose.yaml -f compose.dify.yaml up -d --build
```

这只会把 SAG 的 `api` 服务加入指定网络，并在该网络中提供主机别名 `sag`。
不要把 Dify 兼容端点直接暴露到公网。

## 3. 配置 Dify 的容器访问策略

Dify 的外部知识库请求经过 SSRF Proxy。请在 Dify 的 `docker/.env` 中设置：

```dotenv
SSRF_PROXY_ALLOW_PRIVATE_DOMAINS=sag
SSRF_DEFAULT_TIME_OUT=30
SSRF_DEFAULT_READ_TIME_OUT=30
```

`SSRF_DEFAULT_TIME_OUT` 和 `SSRF_DEFAULT_READ_TIME_OUT` 都必须大于 SAG 的
单信源检索超时，并预留网络开销。Dify 会单独使用读取超时；只提高总超时仍可能
在默认 5 秒后中断。启用默认的 `multi → vector` 回退时，一次请求最坏可能连续
消耗两次单信源超时，因此建议两个 Dify 超时都大于
`2 × SAG_SEARCH_SOURCE_TIMEOUT + 网络余量`。默认 SAG 单源超时为 12 秒时可设
30 秒；如果 SAG 提高到 45 秒，Dify 建议至少设为 100 秒。

重新创建 Dify 的相关服务使配置生效：

```bash
docker compose up -d --force-recreate api ssrf_proxy
```

这里仅修改部署者自己的 Dify 配置，不需要改动 Dify 源码。

## 4. 查找 SAG Source ID

打开 SAG 的目标信源页面，地址格式为：

```text
http://localhost:3000/knowledge/<SOURCE_ID>
```

最后一段 `<SOURCE_ID>` 就是 Dify 要填写的 Knowledge ID。也可以使用带 SAG
用户 JWT 的 `GET /api/v1/sources` 查询信源列表及其 `id`。

## 5. 在 Dify 创建外部知识库

1. 进入 **知识库 → 创建知识库 → 连接外部知识库**。
2. 新建外部知识 API：
   - 名称：例如 `SAG`
   - Endpoint：`http://sag:8000/api/v1/dify`
   - API Key：`make setup-dify` 输出的密钥
3. 创建外部知识库，将 Knowledge ID 填为目标 SAG `source.id`。
4. 使用命中测试，或在应用中添加知识检索节点验证结果和引用。

Dify 会自动在 Endpoint 后追加 `/retrieval`，所以 Endpoint 中不要重复填写
`/retrieval`，也不要把容器内 HTTP 地址误写成 HTTPS。

## 检索协议

Dify 最终调用：

```http
POST /api/v1/dify/retrieval
Authorization: Bearer <SAG_DIFY_API_KEY>
Content-Type: application/json
```

请求示例：

```json
{
  "knowledge_id": "<SAG_SOURCE_ID>",
  "query": "星河计划的识别码是什么",
  "retrieval_setting": {
    "top_k": 5,
    "score_threshold": 0.3
  }
}
```

兼容端点固定使用 SAG `multi` 检索，并沿用 SAG 已有的超时、失败或空结果时
回退 `vector` 的行为。返回的 `records` 带有 `source_id`、`source_name`、
`chunk_id`、`heading` 和 `document_id`，用于 Dify 引用溯源。

## 当前限制

- `metadata_condition` 首版不支持，传入时返回 422，不会静默忽略。
- 一个 Dify 外部知识库只绑定一个 SAG `source.id`。
- SAG 当前是单用户产品，不将此接口作为跨租户知识隔离边界。
- 该接口只提供检索证据，不负责从 Dify 上传、更新或删除 SAG 文档。
