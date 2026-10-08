# sag-api

sag 的后端服务：FastAPI + `zleap-sag`。

## 分层

| 层 | 目录 | 职责 |
|---|---|---|
| 适配层 | `sag_api/sag/` | **唯一** import `zleap-sag` 之处；信源 ↔ `DataEngine` |
| 连接器 | `sag_api/connectors/` | 采集抽象 + 注册表（文件上传 → 动态同步） |
| 文档解析 | `sag_api/parsing/` | Markdown 直通；PDF 优先 MinerU、失败自动回退；`anydoc` 模式本机转换 DOCX/PPTX/EPUB/PDF/CSV（`parsing/anydoc.py`）；Excel 与其余格式由 MarkItDown 转换 |
| 任务队列 | `sag_api/jobs/` | 后台处理编排（ingest → extract 状态机） |
| 生成层 | `sag_api/generation/` | 检索结果 → LLM 流式答案 + 引用 |
| 工具层 | `sag_api/tools/` | Agent 工具：内置检索/实体 + 远端 MCP 适配（统一 `Tool` 接口） |
| Agent Core | `sag_agent/` | 独立编排核心：生命周期、事件、工具、审批、取消、存储端口 |
| Agent 适配 | `sag_api/services/agent_service.py` | 将 SAG 模型、工具、会话接入 Agent Core |
| MCP | `sag_api/mcp/` | 信源即 MCP：FastMCP server + Streamable-HTTP 挂载（`/mcp/`）+ stdio 入口 |
| 领域服务 | `sag_api/services/` | 纯业务逻辑，不依赖 FastAPI |
| 接口 | `sag_api/api/v1/` | HTTP 路由，仅做 IO / 校验 / 序列化 |

## 运行

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
uvicorn sag_api.main:app --reload --host 0.0.0.0 --port 8000
```

文档 UI：http://localhost:8000/docs

也可以在仓库根目录运行 `make api`。开发服务器默认监听全部本机网卡，便于从局域网地址访问 Web；生产环境请通过反向代理与访问控制暴露服务。

## Document extraction progress

Document progress reserves the first 20% for preparation: conversion/parsing,
chunking, and chunk embeddings. Loading and parser markers remain at 5% and 10%.
Extraction starts at 20% and advances through the remaining 80% using
`20 + round(80 * completed / total)` (for example, 811 of 2033 shows 52%, and
half the chunks shows 60%). Background jobs report each durable chunk completion.
Only changed percentages are committed, and job progress
uses the same percentage divided by 100. The bar stays at 99% until the batch
has saved its events and graph vectors, then reaches 100% after success.

Failed or paused documents retain their last percentage. Background document jobs
save each successful chunk result before advancing durable extraction progress.
Restart recovery restores the saved fraction before dispatching the job. Pause,
automatic retry and explicit resume reuse saved results, including valid empty
results; unfinished chunks run again. At concurrency one, a crash can repeat one
in-flight chunk. Higher concurrency can repeat every in-flight chunk.

The existing parsing/ingest path remains in use, including spreadsheet original
bytes. Checkpoints work with ordinary ingested chunks whose `generation_id` is
null. New events remain unpublished until every chunk succeeds and the existing
whole-batch graph/vector commit finishes. Contract violations still fail the
batch; successful checkpoints survive that failure. Progress stays below 100%
until publication succeeds, even when all chunk results are already saved.

`document_extraction_checkpoints` stores one immutable JSON outcome per successful
chunk in the application database (SQLite locally, PostgreSQL in deployed use).
`Job.payload` stores the run identity, fingerprint, fixed prompt clock,
existing chunk references and a publication cleanup journal containing vector IDs.
A failed job retried through a new job retains that
run identity. Ready-document reprocessing starts a fresh run and removes old
checkpoints; document deletion cascades their removal. Startup creates the new
table through the existing database initialization path. Retain this table in
database backups along with jobs and documents; no local checkpoint files exist.

Replay verifies model/embedding configuration (excluding API keys), extraction
options, prompt definitions, entity contracts, article title, referenced section
contents and chunk fields, including the length used to skip short chunks. A mismatch
refuses reuse and asks the operator to restore the configuration or upload again.
It never mixes results from different configurations. Existing jobs which predate
this feature can checkpoint future work; their lost in-memory results cannot be
recovered. A saved success is counted only after its transaction commits. Display
write failures do not fail extraction; checkpoint write failures do. Incomplete
checkpoints created by the earlier fingerprint format fail closed because their
section/title inputs cannot be verified retrospectively. Finish those jobs with
the previous version before upgrading, or upload them again.

Before any vector mutation, publication commits its event, association and newly
created entity IDs to the application checkpoint. It then reacquires and verifies
the job claim; a pause or claim transfer during that gap aborts before vector writes.
Publication holds that claim until relation/vector writes and the durable completion
marker finish. Claim transfers and pause/delete controls wait for the mutation
phase; repeated cancellation drains the transaction. On SQLite this
also holds the application's database write lock during publication.
Metadata callbacks lock document then job, matching control transitions to avoid
opposite lock ordering on PostgreSQL.
Worker exits roll back any remaining metadata fence before the separate lease
cleanup transaction, including scheduler yield, deletion and cancellation. Relation
changes stay in one transaction until vectors finish. Replay replaces only the
saved event IDs, avoiding primary-key collisions after publication succeeded but
the application acknowledgement was lost. Failed acknowledgement writes remain
retryable, including an uncertain application commit. A committed marker skips publication;
a READY document skips extraction and source-counter increments on job retry.
Startup also restores progress for paused jobs without dispatching them.

Relations and vectors remain separate stores. A handled vector failure uses
zleap's compensation and rolls back the relation transaction. Abrupt process
death during vector writes can leave partial vector changes; the old relation
snapshot remains intact until publication commits. Retry first removes journaled
vectors whose relations rolled back, preserving entities still referenced by durable
relations, then republishes cached outcomes. Cleanup failures retain the journal
and prevent acknowledgement. Document deletion uses the same journal in its source
maintenance window before retiring the document. Success clears the journal in
the same commit as the completion marker. Transient relation database failures
remain eligible for the stock queue's retry policy, including uncertain commit results
and disconnects wrapped by the saver's failed compensation. The original database
cause determines retryability whether compensation succeeds or fails; integrity
and configuration errors remain failures. Journals added by this version
cannot identify orphan vectors left by a crash on an older, unjournaled version.
This is not an atomic transaction across both stores.
Finish or delete jobs with pending publication journals using this version before
downgrading to a version that does not understand those journals.

The pinned `zleap-sag==0.13.0` explicit extract API does not emit chunk progress.
For calls outside background jobs without a checkpoint store, the document
processor temporarily installs its extractor's `_on_progress`
callback, serializes calls sharing that extractor, and restores the previous
callback on success, failure, pause, or cancellation. Public stage progress, when
available, uses the same display callback. On a zleap upgrade, review this seam and
run `tests/test_document_progress.py` and `tests/test_document_resume.py`; the
progress suite exercises the installed extractor with fake chunk results and
checks database writes, checkpoint isolation, and concurrent callback ownership.

Background jobs instead use a request-owned copy of the engine/adapter and the
installed extractor's `prepare_batch(cached_chunks, on_chunk_result, on_progress)`
and `commit_prepared` boundaries. Shared engine state is untouched. zleap's
managed operation writer cannot attach to an ordinary ingest checkpoint, so this
path does not create artificial operation rows or generation IDs. Separate chunk
rows avoid repeatedly rewriting a growing event blob: a synthetic 2,033-chunk
fixture with one roughly 1 KB outcome per chunk would serialize about 2.2 GB
cumulatively as a growing JSON blob, compared with about 2.2 MB of chunk outcomes.
This is a sizing example, not a production benchmark.

On dependency upgrades, review `_extract_adapter`, `_extractor`, `_config`, built-in
adapter event serialization, prompt/entity fingerprints, the saver relation/vector
ordering, publication journal, rollback-error causes and borrowed-session commit behavior.
Run `tests/test_document_extraction.py`
alongside the progress, resume,
retry, parsing and spreadsheet suites. Crash/replay tests use real zleap batch
preparation, the real relation writer and database checkpoints with deterministic
model/vector transports. They kill processes before and after relation, vector,
publication and acknowledgement boundaries;
live provider acceptance requires separate validation. Custom extract adapters
must provide a supported durable integration; background jobs fail clearly rather
than silently reverting to extraction without checkpoints.
