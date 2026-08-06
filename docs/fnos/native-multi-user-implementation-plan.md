# SAG fnOS Native Multi-User Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将现有 fnOS Docker 版 SAG 改造成由 fnOS 统一网关访问的 Native 应用，并让每个 fnOS UID 自动获得独立的 SQLite、LanceDB、上传目录和后台 Worker。

**Architecture:** 一个共享的 Native Gateway 监听 `${TRIM_APPDEST}/app.sock`，验证 fnOS 网关 Header，并把业务请求转发给按 UID 启动的 SAG Worker；每个 Worker 在进程启动前获得固定的数据库和数据目录环境变量。第一阶段保留共享 Next.js standalone 进程，页面请求由 Gateway 转发，API/SSE/MCP 请求由 Gateway 转发到用户 Worker；不再使用 Nginx 或 Docker Compose。

**Tech Stack:** Python 3.12、FastAPI/Starlette、Uvicorn Unix Socket、HTTPX、WebSockets、SQLAlchemy/SQLite、LanceDB、Node.js 22、Next.js 15、fnpack、Node.js test runner、pytest、Vitest、GitHub Actions。

## Global Constraints

- 生产入口只能是 fnOS 统一网关 `/app/sag`；不得暴露 API、Next 或 Worker 的 NAS TCP 端口。
- 生产请求缺少合法 `X-Trim-Userid` 时必须拒绝；不得回退为 `local`、默认用户或匿名单用户。
- 每个 fnOS UID 首次访问时创建空工作区；没有旧 Docker 数据迁移、公共知识库或跨用户共享。
- UID 是工作区主键；用户名只用于显示，改名不得创建新工作区。
- 每个 Worker 固定使用 `${TRIM_PKGVAR}/users/<uid>/meta/sag.db`、`engine/` 和 `uploads/`；业务请求不得传入数据库路径。
- Native 长期进程使用 `run-as=package`，用户名和组名均为 `sag`。
- 第一阶段运行时依赖为 `python312:nodejs_v22`；静态前端完成后才能移除 `nodejs_v22`。
- x86 是当前正式发布目标，manifest 使用 `platform=x86`；ARM 保留为后续兼容目标，恢复发布前必须使用 `platform=arm`，禁止 `platform=all`。
- 最低 fnOS 版本为 `1.2.0302`，直到真机验证得出更高的必要下限。
- 完整功能版最终 FPK 硬上限：x86 285 MiB、ARM 260 MiB；安装和首次启动不得访问 PyPI、npm registry、Google Fonts 或容器镜像仓库。
- 单文件上传上限保持 25 MiB；默认同时运行 Worker 上限为 4，空闲回收时间为 15 分钟，活跃任务和流式连接禁止回收。
- 所有新行为必须先写失败测试，再实现最小通过代码；每个任务独立提交。
- Task 2 的 x86 真机 P0 证据通过前，不得开始 Task 3；ARM 真机 P0 验证不阻塞 x86 正式发布。

---

## 0. Execution Order and File Map

按以下依赖执行，不允许跳过硬门槛：

```text
Task 1 Native 构建骨架
  -> Task 2 x86 真机 P0
    -> Task 3 fnOS 身份模式
      -> Task 4 用户路径与 Worker 入口
        -> Task 5 Worker Supervisor
          -> Task 6 Gateway HTTP/SSE 代理
            -> Task 7 前端 /app/sag 适配
              -> Task 8 Native 包生命周期
                -> Task 9 多用户端到端隔离
                  -> Task 10 备份、恢复与空间保护
                    -> Task 11 x86 vendor 与体积门禁
                      -> Task 12 Native 发布流水线
                        -> Task 13 全量验证与交付切换
```

最终文件职责：

| 文件 | 单一职责 |
| --- | --- |
| `apps/api/sag_api/fnos/identity.py` | 解析 fnOS Header；签名和校验 Gateway 到 Worker 的内部身份 |
| `apps/api/sag_api/fnos/workspace.py` | 从 UID 生成并安全创建私有路径 |
| `apps/api/sag_api/fnos/worker.py` | 在导入 SAG 单例配置前设置用户环境并启动 UDS Worker |
| `apps/api/sag_api/fnos/supervisor.py` | Worker 启动、租约、健康检查、回收和关闭 |
| `apps/api/sag_api/fnos/proxy.py` | 过滤 Header、重写路径、流式转发 HTTP/SSE |
| `apps/api/sag_api/fnos/gateway.py` | 组合身份、Supervisor、API 代理和 Next 页面代理 |
| `apps/api/sag_api/fnos/cli.py` | Native Gateway 命令行入口 |
| `apps/api/sag_api/services/fnos_user_service.py` | 在用户私库幂等创建/更新唯一 fnOS 用户 |
| `apps/api/sag_api/api/v1/fnos_internal.py` | 返回 Worker 活跃任务状态，供 Supervisor 回收判断 |
| `apps/web/lib/deployment.ts` | `/app/sag` 页面和 API 路径的唯一构造入口 |
| `packages/fnos/native/sag/` | 不含 vendor 的 Native fnpack 模板 |
| `scripts/build-fnos-native-probe.mjs` | 生成 P0 专用 vendor 并构建双架构探针 FPK |
| `scripts/build-fnos-native-vendor.mjs` | 按架构生成锁定 Python vendor |
| `scripts/build-fnos-native-package.mjs` | 渲染模板、复制 vendor/Web 并调用 fnpack |
| `scripts/validate-fnos-native-package.mjs` | 包结构、权限、架构、离线和体积验证 |
| `scripts/fnos-native-size-report.mjs` | 输出稳定 JSON 体积报告和增量门禁 |

---

### Task 1: Native Package Skeleton and Structural Contract

**Files:**
- Create: `packages/fnos/native/sag/manifest`
- Create: `packages/fnos/native/sag/config/privilege`
- Create: `packages/fnos/native/sag/config/resource`
- Create: `packages/fnos/native/sag/app/ui/config`
- Create: `packages/fnos/native/sag/app/ui/images/icon_64.png`
- Create: `packages/fnos/native/sag/app/ui/images/icon_256.png`
- Create: `packages/fnos/native/sag/ICON.PNG`
- Create: `packages/fnos/native/sag/ICON_256.PNG`
- Create: `packages/fnos/native/sag/wizard/uninstall_uifile`
- Create: `scripts/validate-fnos-native-package.mjs`
- Create: `scripts/build-fnos-native-probe.mjs`
- Create: `scripts/tests/fnos-native-package.test.mjs`
- Reuse assets from: `packages/fnos/sag/ICON.PNG`, `packages/fnos/sag/ICON_256.PNG`, `packages/fnos/sag/app/ui/images/`

**Interfaces:**
- Produces: `validateNativeTemplate(root: string, platform: "x86" | "arm"): Promise<void>`.
- Produces CLI: `build-fnos-native-probe.mjs --platform x86|arm --output <fpk>`；只用于 Task 2 的兼容性阻断验证。
- Produces: 一个无 Docker 资源、声明统一网关和包用户的 fnpack 模板，供 Task 8 和 Task 11 渲染。

- [ ] **Step 1: Write failing structural tests**

```js
test("native template uses package user and no Docker project", async () => {
  const privilege = JSON.parse(await readFile(path.join(template, "config/privilege")));
  const resource = JSON.parse(await readFile(path.join(template, "config/resource")));
  assert.equal(privilege.defaults["run-as"], "package");
  assert.equal(privilege.username, "sag");
  assert.equal(Object.hasOwn(resource, "docker-project"), false);
});

test("native template exposes only the fnOS gateway", async () => {
  const ui = JSON.parse(await readFile(path.join(template, "app/ui/config")));
  const entry = ui[".url"]["sag.Application"];
  assert.equal(entry.gatewayPrefix, "/app/sag");
  assert.equal(entry.gatewaySocket, "app.sock");
  assert.equal(entry.allUsers, true);
});
```

- [ ] **Step 2: Run tests and confirm the missing template fails**

Run: `node --test scripts/tests/fnos-native-package.test.mjs`

Expected: FAIL because `packages/fnos/native/sag` and the validator do not exist.

- [ ] **Step 3: Create the minimal template**

Use this manifest, with `__SAG_VERSION__` and `__SAG_PLATFORM__` intentionally reserved for the builder:

```ini
appname=sag
version=__SAG_VERSION__
display_name=SAG知识库
desc=Self-hosted AI knowledge base and research workspace
platform=__SAG_PLATFORM__
os_min_version=1.2.0302
source=thirdparty
maintainer=Zleap AI
distributor=Zleap AI
install_dep_apps=python312:nodejs_v22
ctl_stop=true
desktop_uidir=ui
desktop_applaunchname=sag.Application
```

Use `{}` for `config/resource`. The validator must reject `docker-project`, `service_port`, `run-as=root`, `platform=all`, unresolved `__SAG_*__` tokens, missing icons, and any `docker-compose.yaml` under the rendered Native package.

Implement the probe builder in the same task. It must create a temporary render directory, export the locked API requirements from `apps/api/uv.lock`, install Linux CPython 3.12 wheels with `uv pip install --python-platform x86_64-unknown-linux-gnu` or `aarch64-unknown-linux-gnu` into `server/vendor`, and copy `scripts/fnos-native-probe.py` to `server/`. It generates `cmd/main` with these exact behaviors: `start` launches `python3 "$TRIM_APPDEST/server/fnos-native-probe.py" serve --socket "$TRIM_APPDEST/app.sock" --output "$TRIM_PKGVAR/native-p0.json"` and records its PID; `status` verifies that PID and Socket; `stop` sends TERM, waits at most 15 seconds, then sends KILL. It then replaces both manifest tokens, calls `fnpack build`, runs the Native validator, and always removes the temporary directory. It must refuse an output path without the `.fpk` suffix and must not reuse the production vendor cache introduced in Task 11.

- [ ] **Step 4: Run the structural test**

Run: `node --test scripts/tests/fnos-native-package.test.mjs`

Expected: PASS with the x86 and ARM fixture renders; the negative cases reject root, Docker, `platform=all`, and unresolved tokens.

- [ ] **Step 5: Commit**

```bash
git add packages/fnos/native/sag scripts/validate-fnos-native-package.mjs scripts/build-fnos-native-probe.mjs scripts/tests/fnos-native-package.test.mjs
git commit -m "feat(fnos): add native package contract"
```

---

### Task 2: x86 P0 Compatibility Gate

**Files:**
- Create: `scripts/fnos-native-probe.py`
- Create: `scripts/tests/fnos-native-probe.test.mjs`
- Create during device execution: `docs/fnos/evidence/native-p0-x86.json`
- Create during later ARM device execution: `docs/fnos/evidence/native-p0-arm.json`

**Interfaces:**
- Produces: `python3 scripts/fnos-native-probe.py serve --socket <uds> --output <json>` with stable keys `python`, `machine`, `imports`, `lancedb_roundtrip`, `uds_http`, `gateway_headers`, and `status`.
- Probe endpoint: `GET /probe` records the three fnOS identity Headers and returns the complete JSON result.
- Gate: `docs/fnos/evidence/native-p0-x86.json` must contain `"status": "pass"`; otherwise implementation stops after committing the evidence and failure explanation. ARM evidence is deferred and does not block the x86 release path.

- [ ] **Step 1: Write the probe contract test**

```js
test("probe requires every native dependency and UDS behavior", () => {
  const source = readFileSync(probe, "utf8");
  for (const module of ["lancedb", "pyarrow", "onnxruntime", "numpy", "uvloop", "orjson"])
    assert.match(source, new RegExp(`import ${module}`));
  for (const key of ["lancedb_roundtrip", "uds_http", "gateway_headers", "status"])
    assert.match(source, new RegExp(key));
});
```

- [ ] **Step 2: Implement a deterministic probe**

The probe must:

1. Import all six binary modules.
2. Create a temporary LanceDB table containing two vectors and query one nearest neighbor.
3. Start a temporary Uvicorn app on a Unix Socket and fetch `/ready` through HTTPX UDS transport.
4. Expose `GET /probe`; when invoked through the fnOS gateway, record `X-Trim-Userid`, `X-Trim-Username`, and `X-Trim-Isadmin` in `gateway_headers` and atomically rewrite the result.
5. Run `ldd` on every `.so` under vendor and fail on `not found`.
6. Delete its temporary database and Socket before exit.
7. Write JSON atomically and return 0 only when every check passes.

- [ ] **Step 3: Build and install the x86 probe FPK**

Run on the build host:

```bash
node scripts/build-fnos-native-probe.mjs --platform x86 --output dist/sag-native-probe-x86.fpk
```

Install the x86 FPK on the x86 fnOS device. Start it from fnOS, sign in as a non-root normal user, and open `/app/sag/probe` once through both the HTTP and HTTPS fnOS access domains. Then validate the generated file through the application callback environment:

```bash
/var/apps/python312/target/bin/python3 -c 'import json,os,sys; p=os.path.join(os.environ["TRIM_PKGVAR"],"native-p0.json"); v=json.load(open(p)); sys.exit(0 if v["status"]=="pass" and v["gateway_headers"]["x-trim-userid"] else 1)'
```

- [ ] **Step 4: Record and validate x86 evidence**

Copy the exact generated JSON into the x86 evidence file. Validate:

```bash
node -e 'const v=require("./docs/fnos/evidence/native-p0-x86.json"); if(v.status!=="pass" || v.machine!=="x86_64" || !v.gateway_headers["x-trim-userid"]) process.exit(1)'
```

Expected: exit 0, Python reports 3.12.x, machine reports x86_64, all imports are true, and UDS/LanceDB checks pass.

- [ ] **Step 5: Commit the probe and evidence**

```bash
git add scripts/fnos-native-probe.py scripts/tests/fnos-native-probe.test.mjs docs/fnos/evidence
git commit -m "test(fnos): verify native runtime compatibility"
```

---

### Task 3: Trusted fnOS Identity Mode

**Files:**
- Create: `apps/api/sag_api/fnos/__init__.py`
- Create: `apps/api/sag_api/fnos/identity.py`
- Create: `apps/api/sag_api/services/fnos_user_service.py`
- Create: `apps/api/tests/test_fnos_identity.py`
- Modify: `apps/api/sag_api/core/config.py`
- Modify: `apps/api/sag_api/core/deps.py`
- Modify: `apps/api/sag_api/api/v1/auth.py`

**Interfaces:**
- Produces: `GatewayIdentity(uid: int, username: str, is_admin: bool)`.
- Produces: `parse_gateway_identity(headers: Mapping[str, str]) -> GatewayIdentity`.
- Produces: `InternalIdentitySigner.sign(identity, request_id, now) -> dict[str, str]`.
- Produces: `InternalIdentitySigner.verify(headers, expected_uid, now) -> GatewayIdentity`.
- Produces: `InternalIdentitySigner.from_file(path: Path, max_age_seconds=30) -> InternalIdentitySigner`；仅接受 mode `0600`、64 位小写十六进制密钥文件，并用 `bytes.fromhex()` 解码为 32-byte HMAC key。
- Produces: `get_or_create_fnos_user(session, identity) -> User`.
- Modifies: `Settings.auth_mode` to accept `"fnos"`; adds `fnos_uid: int | None`, `fnos_internal_secret_file: str`.

- [ ] **Step 1: Write failing identity tests**

```python
def test_gateway_identity_uses_uid_not_username():
    identity = parse_gateway_identity({
        "x-trim-userid": "1000",
        "x-trim-username": "Alice",
        "x-trim-isadmin": "false",
    })
    assert identity == GatewayIdentity(uid=1000, username="Alice", is_admin=False)

def test_internal_signature_rejects_uid_substitution(tmp_path):
    signer = InternalIdentitySigner(b"s" * 32, max_age_seconds=30)
    headers = signer.sign(GatewayIdentity(1000, "Alice", False), "req-1", 100)
    headers["x-sag-internal-uid"] = "1001"
    with pytest.raises(AuthError):
        signer.verify(headers, expected_uid=1000, now=100)
```

Add tests for missing UID, non-decimal UID, UID 0, username over 120 UTF-8 characters, invalid admin value, signature older than 30 seconds, future timestamp over 5 seconds, duplicate logical Header values, and request ID over 128 characters.

- [ ] **Step 2: Run the focused tests**

Run: `cd apps/api && uv run --extra dev pytest tests/test_fnos_identity.py -q`

Expected: FAIL with missing `sag_api.fnos.identity`.

- [ ] **Step 3: Implement the exact signature protocol**

Canonical bytes are UTF-8 encoding of:

```text
v1\n<timestamp-seconds>\n<request-id>\n<uid>\n<username>\n<0-or-1>
```

Use HMAC-SHA256 and lowercase hexadecimal. Internal Header names are:

```text
X-SAG-Internal-Uid
X-SAG-Internal-Username
X-SAG-Internal-Isadmin
X-SAG-Internal-Timestamp
X-SAG-Internal-Request-Id
X-SAG-Internal-Signature
```

Use `hmac.compare_digest`. Do not accept an external bearer token as a substitute in `fnos` mode.

- [ ] **Step 4: Implement the per-database fnOS user**

Use stable fields:

```python
User(
    id=f"fnos_{identity.uid}",
    email=f"fnos-{identity.uid}@local.invalid",
    password_hash=await hash_password_async(secrets.token_urlsafe(32)),
    password_initialized=False,
    auth_singleton=1,
    name=identity.username,
)
```

On later requests update only `name` when the fnOS username changes. In `fnos` mode, `/auth/session` returns `setup_required=false`; `/auth/register`, `/auth/login`, POST `/auth/session`, and DELETE `/auth/session` return 404.

- [ ] **Step 5: Run identity and legacy auth regression tests**

Run:

```bash
cd apps/api
uv run --extra dev pytest tests/test_fnos_identity.py tests/test_single_user_no_auth.py tests/test_password_auth_sessions.py -q
```

Expected: PASS; existing `single_user` and `password` behavior remains unchanged.

- [ ] **Step 6: Commit**

```bash
git add apps/api/sag_api/fnos apps/api/sag_api/services/fnos_user_service.py apps/api/sag_api/core/config.py apps/api/sag_api/core/deps.py apps/api/sag_api/api/v1/auth.py apps/api/tests/test_fnos_identity.py
git commit -m "feat(fnos): trust signed gateway identity"
```

---

### Task 4: Secure Workspace Paths and Worker Entrypoint

**Files:**
- Create: `apps/api/sag_api/fnos/workspace.py`
- Create: `apps/api/sag_api/fnos/worker.py`
- Create: `apps/api/tests/test_fnos_workspace.py`
- Create: `apps/api/tests/test_fnos_worker_entrypoint.py`

**Interfaces:**
- Produces: `WorkspacePaths.for_uid(data_root: Path, temp_root: Path, uid: int) -> WorkspacePaths`.
- Produces fields: `root`, `meta_dir`, `database_file`, `engine_dir`, `uploads_dir`, `logs_dir`, `socket_file`.
- Produces: `WorkspacePaths.prepare() -> None` with directory mode `0700` and symlink refusal.
- Produces CLI: `python -m sag_api.fnos.worker --uid <uid> --username <name> --socket <absolute-path>`.

- [ ] **Step 1: Write path safety tests**

```python
def test_workspace_layout_is_uid_scoped(tmp_path):
    paths = WorkspacePaths.for_uid(tmp_path / "data", tmp_path / "tmp", 1000)
    assert paths.database_file == tmp_path / "data/users/1000/meta/sag.db"
    assert paths.engine_dir == tmp_path / "data/users/1000/engine"
    assert paths.uploads_dir == tmp_path / "data/users/1000/uploads"
    assert paths.socket_file == tmp_path / "tmp/workers/1000.sock"

def test_prepare_rejects_symlinked_user_root(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "data/users").mkdir(parents=True)
    (tmp_path / "data/users/1000").symlink_to(outside, target_is_directory=True)
    with pytest.raises(UnsafeWorkspacePath):
        WorkspacePaths.for_uid(tmp_path / "data", tmp_path / "tmp", 1000).prepare()
```

- [ ] **Step 2: Run and confirm failure**

Run: `cd apps/api && uv run --extra dev pytest tests/test_fnos_workspace.py tests/test_fnos_worker_entrypoint.py -q`

Expected: FAIL because workspace and worker modules do not exist.

- [ ] **Step 3: Implement path preparation**

Use `Path.lstat()` on every existing component from `users` down to the UID directory. Reject symlinks and non-directories. Create missing directories with `mode=0o700`; call `chmod(0o700)` after creation. The Worker Socket is under `${TRIM_PKGTMP}/workers`, not inside persistent user data.

- [ ] **Step 4: Implement worker boot ordering**

`worker.py` must parse arguments and set these variables before importing `sag_api.main` or `sag_api.core.config`:

```python
os.environ["SAG_AUTH_MODE"] = "fnos"
os.environ["SAG_FNOS_UID"] = str(uid)
os.environ["SAG_DATABASE_URL"] = f"sqlite+aiosqlite:////{paths.database_file}"
os.environ["SAG_DATA_DIR"] = str(paths.engine_dir)
os.environ["SAG_UPLOAD_DIR"] = str(paths.uploads_dir)
os.environ["SAG_ENGINE_CACHE_SIZE"] = "2"
os.environ["SAG_ENGINE_WARMUP_COUNT"] = "1"
```

Then import `create_app()` and run Uvicorn with `uds=str(paths.socket_file)`, `workers=1`, and proxy headers disabled. Reject a Socket argument different from `WorkspacePaths.socket_file`.

- [ ] **Step 5: Run focused tests**

Run: `cd apps/api && uv run --extra dev pytest tests/test_fnos_workspace.py tests/test_fnos_worker_entrypoint.py -q`

Expected: PASS, including proof that settings read the per-user database URL rather than the parent process value.

- [ ] **Step 6: Commit**

```bash
git add apps/api/sag_api/fnos/workspace.py apps/api/sag_api/fnos/worker.py apps/api/tests/test_fnos_workspace.py apps/api/tests/test_fnos_worker_entrypoint.py
git commit -m "feat(fnos): add isolated user worker entrypoint"
```

---

### Task 5: Worker Supervisor and Lease State Machine

**Files:**
- Create: `apps/api/sag_api/fnos/supervisor.py`
- Create: `apps/api/tests/test_fnos_supervisor.py`
- Create: `apps/api/sag_api/api/v1/fnos_internal.py`
- Modify: `apps/api/sag_api/api/v1/__init__.py`

**Interfaces:**
- Produces: `WorkerHandle(uid, process, socket_file, client, last_activity, in_flight, streams)`.
- Produces: `WorkerLease(handle)` as an async context manager.
- Produces: `WorkerSupervisor.acquire(identity) -> WorkerLease`.
- Produces: `WorkerSupervisor.reap_idle(now: float) -> list[int]`.
- Produces: `WorkerSupervisor.close() -> None`.
- Produces authenticated endpoint: `GET /api/v1/fnos-internal/worker-status -> {queued, running, active}`.

- [ ] **Step 1: Write state-machine tests using a fake process factory**

```python
@pytest.mark.asyncio
async def test_concurrent_first_access_spawns_once(supervisor):
    leases = await asyncio.gather(*[
        supervisor.acquire(GatewayIdentity(1000, "Alice", False))
        for _ in range(20)
    ])
    assert supervisor.process_factory.spawn_count == 1
    for lease in leases:
        await lease.release()

@pytest.mark.asyncio
async def test_reaper_keeps_busy_worker(supervisor, clock):
    lease = await supervisor.acquire(GatewayIdentity(1000, "Alice", False))
    clock.advance(901)
    assert await supervisor.reap_idle(clock.now()) == []
    await lease.release()
```

Add tests for maximum four Workers, fifth user returning `WorkerCapacityError`, failed startup cleaning only its Socket, streamed lease preventing reaping, active job preventing reaping, graceful TERM followed by KILL after 15 seconds, and `close()` stopping every known child.

- [ ] **Step 2: Run and confirm failure**

Run: `cd apps/api && uv run --extra dev pytest tests/test_fnos_supervisor.py -q`

Expected: FAIL with missing supervisor.

- [ ] **Step 3: Implement per-UID locks and leases**

Use one `asyncio.Lock` for the registry and one start lock per UID. Never await process startup while holding the global registry lock. `acquire()` increments `in_flight` before returning. `WorkerLease.release()` decrements once and updates `last_activity`.

Spawn with an explicit executable and environment:

```python
await asyncio.create_subprocess_exec(
    sys.executable, "-m", "sag_api.fnos.worker",
    "--uid", str(identity.uid),
    "--username", identity.username,
    "--socket", str(paths.socket_file),
    env=worker_env,
    start_new_session=True,
)
```

Wait up to 60 seconds for Socket plus `/api/v1/system/ready`. Capacity exhaustion maps to HTTP 503 with `Retry-After: 5`.

- [ ] **Step 4: Implement job-aware reaping endpoint**

Query `Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING])`; return counts only. Require signed fnOS identity and reject non-fnOS auth modes with 404. Reaper may stop only when `in_flight == 0`, `streams == 0`, and `active == 0`.

- [ ] **Step 5: Run tests**

Run: `cd apps/api && uv run --extra dev pytest tests/test_fnos_supervisor.py tests/test_document_job_retry.py -q`

Expected: PASS and no regression in job retry behavior.

- [ ] **Step 6: Commit**

```bash
git add apps/api/sag_api/fnos/supervisor.py apps/api/sag_api/api/v1/fnos_internal.py apps/api/sag_api/api/v1/__init__.py apps/api/tests/test_fnos_supervisor.py
git commit -m "feat(fnos): supervise per-user workers"
```

---

### Task 6: Native Gateway HTTP and SSE Proxy

**Files:**
- Create: `apps/api/sag_api/fnos/proxy.py`
- Create: `apps/api/sag_api/fnos/gateway.py`
- Create: `apps/api/sag_api/fnos/cli.py`
- Create: `apps/api/tests/test_fnos_gateway.py`

**Interfaces:**
- Produces: `rewrite_worker_path(path: str, prefix="/app/sag") -> str`.
- Produces: `filtered_request_headers(headers) -> list[tuple[bytes, bytes]]`.
- Produces: `proxy_websocket(client_ws, worker_socket, upstream_path, signed_headers) -> None`.
- Produces: `create_gateway_app(supervisor, signer, web_origin, prefix="/app/sag") -> FastAPI`.
- Produces CLI: `python -m sag_api.fnos.cli gateway --socket <path> --web-origin http://127.0.0.1:3091`.

- [ ] **Step 1: Write proxy contract tests**

```python
@pytest.mark.parametrize(("incoming", "upstream"), [
    ("/app/sag/api/v1/sources", "/api/v1/sources"),
    ("/app/sag/mcp/", "/mcp/"),
])
def test_worker_path_rewrite(incoming, upstream):
    assert rewrite_worker_path(incoming) == upstream

@pytest.mark.asyncio
async def test_sse_is_forwarded_incrementally(gateway_client, worker):
    async with gateway_client.stream("GET", "/app/sag/api/v1/test-stream", headers=FNOS_A) as response:
        first = await anext(response.aiter_bytes())
        assert first.startswith(b"event:")
        assert worker.second_chunk_released is False
```

Add tests proving that `/app/sag/api` and `/app/sag/mcp` require UID, page requests require UID, query strings survive, 25 MiB request bodies stream without full buffering, duplicate `Content-Length` is rejected, external `X-SAG-Internal-*` and `Authorization` are removed, `Set-Cookie` is not introduced, and Worker 404 stays 404.

Add a WebSocket test that sends one text frame and one binary frame through `/app/sag/api/v1/test-ws`, verifies both reach the Worker unchanged, and verifies the Worker close code is propagated to the client.

- [ ] **Step 2: Run and confirm failure**

Run: `cd apps/api && uv run --extra dev pytest tests/test_fnos_gateway.py -q`

Expected: FAIL with missing proxy and gateway.

- [ ] **Step 3: Implement Header and path policy**

Strip hop-by-hop Headers: `connection`, `keep-alive`, `proxy-authenticate`, `proxy-authorization`, `te`, `trailer`, `transfer-encoding`, `upgrade`, and external `x-sag-internal-*`. Reject multiple Host or Content-Length values. Do not forward `Authorization` in fnOS mode. Add only signed internal identity and a stable `X-Request-Id`.

Route policy:

```text
/app/sag/api/* -> user Worker /api/*
/app/sag/mcp/* -> user Worker /mcp/*
/app/sag/*     -> shared Next origin, path unchanged
```

Use `httpx.AsyncHTTPTransport(uds=worker.socket_file)` for Workers and one bounded shared TCP client for Next. Use `StreamingResponse` over `response.aiter_raw()` and close the upstream response in a background callback.

For WebSockets, accept only after identity verification and Worker acquisition. Connect with `websockets.unix_connect(str(worker.socket_file), uri=f"ws://localhost{upstream_path}", additional_headers=signed_headers)`, relay text and binary frames in two cancellable tasks, propagate close codes, and hold `WorkerLease.streams` for the entire connection. Any upgrade request outside `/app/sag/api/*` or `/app/sag/mcp/*` closes with code 1008.

- [ ] **Step 4: Implement Gateway lifecycle**

Gateway lifespan starts the idle reaper at 30-second intervals and closes the Supervisor and HTTP clients on shutdown. `/healthz` is available only through the local Socket and returns Gateway status without user data. Uvicorn must use `proxy_headers=False` and `forwarded_allow_ips=""`.

- [ ] **Step 5: Run proxy and SSE regressions**

Run:

```bash
cd apps/api
uv run --extra dev pytest tests/test_fnos_gateway.py tests/test_search_stream.py tests/test_ask_stream.py -q
```

Expected: PASS; first SSE chunk reaches the client before the upstream finishes.

- [ ] **Step 6: Commit**

```bash
git add apps/api/sag_api/fnos/proxy.py apps/api/sag_api/fnos/gateway.py apps/api/sag_api/fnos/cli.py apps/api/tests/test_fnos_gateway.py
git commit -m "feat(fnos): add native gateway proxy"
```

---

### Task 7: Next.js `/app/sag` and fnOS Session Experience

**Files:**
- Create: `apps/web/lib/deployment.ts`
- Create: `apps/web/lib/deployment.test.ts`
- Modify: `apps/web/next.config.mjs`
- Modify: `apps/web/lib/api.ts`
- Modify: `apps/web/lib/api-base.test.ts`
- Modify: `apps/web/app/(auth)/login/page.tsx`
- Modify: `apps/web/components/features/app-shell.tsx`
- Modify: `apps/web/app/fonts.ts`
- Modify: `apps/web/app/globals.css`

**Interfaces:**
- Produces: `APP_BASE_PATH`, equal to normalized `NEXT_PUBLIC_APP_BASE_PATH` without trailing slash.
- Produces: `appPath(path: string) -> string`.
- Native build inputs: `NEXT_PUBLIC_APP_BASE_PATH=/app/sag`, `NEXT_PUBLIC_API_BASE=/app/sag`.

- [ ] **Step 1: Write failing deployment tests**

```ts
it("prefixes fnOS routes exactly once", async () => {
  process.env.NEXT_PUBLIC_APP_BASE_PATH = "/app/sag/";
  const { appPath } = await import("./deployment");
  expect(appPath("/login")).toBe("/app/sag/login");
  expect(appPath("/app/sag/chat")).toBe("/app/sag/chat");
});

it("uses the fnOS prefix as API base", async () => {
  process.env.NEXT_PUBLIC_API_BASE = "/app/sag";
  expect(await attachmentUrl()).toBe("/app/sag/api/v1/attachments/document-id");
});
```

- [ ] **Step 2: Run and confirm failure**

Run: `cd apps/web && npm run test:unit -- lib/deployment.test.ts lib/api-base.test.ts`

Expected: FAIL because deployment utility and prefixed behavior are absent.

- [ ] **Step 3: Implement base path and redirects**

Set `basePath` from `NEXT_PUBLIC_APP_BASE_PATH` in `next.config.mjs`. Replace literal browser redirects `"/login"` and router paths with `appPath()`. Build `API_BASE` so `/app/sag` remains same-origin and is not replaced by the LAN `:8000` fallback.

The login page continues to call `/auth/session`; in fnOS mode the Worker-created user makes `setup_required=false`, so it immediately redirects to `/app/sag/chat`. Do not display the name form in a Native build after a successful fnOS session response.

- [ ] **Step 4: Make fonts offline**

Remove `next/font/google` so the build never downloads fonts. Keep the existing CSS variable names, but define them as system-font stacks in `globals.css`: `--font-inter: Inter, ui-sans-serif, system-ui, sans-serif` and `--font-jbmono: "JetBrains Mono", ui-monospace, SFMono-Regular, monospace`. `fonts.ts` must export an empty `fontVars` string so the root layout call site remains stable. The production build must succeed with network disabled after `npm ci`.

- [ ] **Step 5: Run frontend verification**

Run:

```bash
cd apps/web
npm run test:unit -- lib/deployment.test.ts lib/api-base.test.ts lib/auth.test.ts lib/login.test.ts
npm run typecheck
NEXT_PUBLIC_APP_BASE_PATH=/app/sag NEXT_PUBLIC_API_BASE=/app/sag npm run build
```

Expected: PASS; `.next/standalone` exists and built HTML/JS contains `/app/sag` but no `fonts.googleapis.com` or `localhost:8000`.

- [ ] **Step 6: Commit**

```bash
git add apps/web
git commit -m "feat(fnos): run web under gateway prefix"
```

---

### Task 8: Native Lifecycle and Runtime Package

**Files:**
- Create: `packages/fnos/native/sag/cmd/install_init`
- Create: `packages/fnos/native/sag/cmd/install_callback`
- Create: `packages/fnos/native/sag/cmd/main`
- Create: `packages/fnos/native/sag/cmd/upgrade_init`
- Create: `packages/fnos/native/sag/cmd/upgrade_callback`
- Create: `packages/fnos/native/sag/cmd/uninstall_init`
- Create: `packages/fnos/native/sag/cmd/uninstall_callback`
- Create: `packages/fnos/native/sag/cmd/config_init`
- Create: `packages/fnos/native/sag/cmd/config_callback`
- Create: `packages/fnos/native/sag/app/runtime/lifecycle.py`
- Create: `scripts/tests/fnos-native-lifecycle.test.mjs`

**Interfaces:**
- `cmd/main start|stop|status` obeys fnOS exit codes 0, 1, 3.
- Runtime paths: Gateway Socket `${TRIM_APPDEST}/app.sock`; Next `127.0.0.1:3091`; PIDs `${TRIM_PKGVAR}/run/gateway.pid` and `web.pid`; logs `${TRIM_PKGVAR}/logs/`.
- Secret file: `${TRIM_PKGETC}/internal-secret`, exactly 64 lowercase hexadecimal characters, mode `0600`.

- [ ] **Step 1: Write lifecycle fixture tests**

Reuse the fake-command pattern from `scripts/tests/fnos-lifecycle.test.mjs`. Assert:

```js
test("start launches loopback Next before the UDS gateway", async (t) => {
  const fixture = await nativeFixture(t);
  const result = runScript("main", fixture.env, ["start"]);
  assert.equal(result.status, 0, result.stderr);
  assert.match(await readFile(fixture.commandLog, "utf8"), /node .*server\.js[\s\S]*python.*sag_api\.fnos\.cli gateway/);
});
```

Add tests for idempotent secret creation, rejection of a symlinked secret, stale PID not killing an unrelated process, readiness failure cleaning spawned processes, status 3 when either service is unhealthy, TERM-before-KILL order, and uninstall retaining data by default.

- [ ] **Step 2: Run and confirm failure**

Run: `node --test scripts/tests/fnos-native-lifecycle.test.mjs`

Expected: FAIL because Native lifecycle scripts are absent.

- [ ] **Step 3: Implement installation and start**

Set:

```bash
export PATH="/var/apps/python312/target/bin:/var/apps/nodejs_v22/target/bin:$PATH"
export PYTHONPATH="${TRIM_APPDEST}/server/vendor:${TRIM_APPDEST}/server"
export SAG_FNOS_DATA_ROOT="${TRIM_PKGVAR}"
export SAG_FNOS_TEMP_ROOT="${TRIM_PKGTMP}"
export SAG_FNOS_INTERNAL_SECRET_FILE="${TRIM_PKGETC}/internal-secret"
```

Start Next with `HOSTNAME=127.0.0.1 PORT=3091 node server.js`; after its HTTP health check passes, start Gateway with `python3 -m sag_api.fnos.cli gateway --socket "${TRIM_APPDEST}/app.sock" --web-origin http://127.0.0.1:3091`. PID files must be written through temporary files and atomic rename.

- [ ] **Step 4: Implement safe stop and status**

Validate that each PID is numeric, belongs to the package user, and its command line contains the exact installed entrypoint before sending signals. Stop Gateway first so no new Worker starts; Gateway closes Workers; then stop Next. Wait 15 seconds before KILL. Remove only verified stale PID and Socket files.

- [ ] **Step 5: Run tests and Bash syntax checks**

Run:

```bash
node --test scripts/tests/fnos-native-lifecycle.test.mjs
for script in packages/fnos/native/sag/cmd/*; do bash -n "$script"; done
```

Expected: PASS with no Docker invocation in command logs.

- [ ] **Step 6: Commit**

```bash
git add packages/fnos/native/sag/cmd packages/fnos/native/sag/app/runtime scripts/tests/fnos-native-lifecycle.test.mjs
git commit -m "feat(fnos): manage native application lifecycle"
```

---

### Task 9: Two-User End-to-End Isolation

**Files:**
- Create: `apps/api/tests/test_fnos_multi_user_isolation.py`
- Modify: `apps/api/tests/conftest.py`
- Modify if required by test evidence: `apps/api/sag_api/fnos/supervisor.py`
- Modify if required by test evidence: `apps/api/sag_api/fnos/gateway.py`

**Interfaces:**
- Consumes: signed identities, `WorkerSupervisor`, Gateway path rewriting and the real SAG API application.
- Produces: a subprocess-level proof that two UIDs cannot enumerate or address each other's resources.

- [ ] **Step 1: Write the end-to-end test**

```python
@pytest.mark.asyncio
async def test_two_fnos_users_receive_disjoint_workspaces(fnos_stack):
    a = fnos_stack.client(headers=FNOS_A)
    b = fnos_stack.client(headers=FNOS_B)
    source = (await a.post("/app/sag/api/v1/sources", json=SOURCE)).json()
    assert (await a.get("/app/sag/api/v1/sources")).json()[0]["id"] == source["id"]
    assert (await b.get("/app/sag/api/v1/sources")).json() == []
    assert (await b.get(f"/app/sag/api/v1/sources/{source['id']}")).status_code == 404
    assert fnos_stack.database(1000) != fnos_stack.database(1001)
```

Extend the test to documents, Agents, threads, messages, settings, jobs, uploads, knowledge search and universe data. Verify identical names do not conflict. Verify admin UID 0 is rejected and an admin Header on UID 1002 creates only UID 1002's private workspace.

- [ ] **Step 2: Run and confirm any integration gaps**

Run: `cd apps/api && uv run --extra dev pytest tests/test_fnos_multi_user_isolation.py -q`

Expected before final fixes: the test identifies any route, startup, signature or cleanup path not covered by unit tests; record the exact failure in the task commit body.

- [ ] **Step 3: Make only evidence-driven integration fixes**

Do not add `owner_id` to business tables. Fix routing, worker startup or cleanup so every UID continues to use a distinct database and data root. A guessed resource ID from another UID must return 404 from that user's independent database.

- [ ] **Step 4: Run isolation and core regressions**

Run:

```bash
cd apps/api
uv run --extra dev pytest tests/test_fnos_multi_user_isolation.py tests/test_api_smoke.py tests/test_agents.py tests/test_document_parsing.py tests/test_document_resume.py tests/test_knowledge_routes.py -q
```

Expected: PASS; two UID directories exist and no API response crosses them.

- [ ] **Step 5: Commit**

```bash
git add apps/api/tests/test_fnos_multi_user_isolation.py apps/api/tests/conftest.py apps/api/sag_api/fnos
git commit -m "test(fnos): prove multi-user data isolation"
```

---

### Task 10: Cold Backup, Restore Safety, and Disk Protection

**Files:**
- Modify: `packages/fnos/native/sag/app/runtime/lifecycle.py`
- Modify: `packages/fnos/native/sag/cmd/upgrade_init`
- Modify: `packages/fnos/native/sag/cmd/upgrade_callback`
- Modify: `packages/fnos/native/sag/cmd/uninstall_callback`
- Create: `scripts/tests/fnos-native-data-lifecycle.test.mjs`

**Interfaces:**
- `lifecycle.py size --root <users>` prints KiB as one decimal-free integer.
- `lifecycle.py backup --root <users> --output <direct-child-of-backup>` creates mode-0600 gzip tar atomically.
- `lifecycle.py validate --archive <file>` rejects traversal, symlinks escaping the archive root, illegal UID directories and corrupt SQLite databases.
- `lifecycle.py delete --root <users>` deletes contents only after explicit `SAG_DELETE_DATA=true`.

- [ ] **Step 1: Write destructive-boundary tests**

Test a two-user fixture containing SQLite WAL files, LanceDB directories, uploads, hidden files and a sparse file. Add hostile archives containing `../escape`, absolute paths, a UID name `alice`, and a symlink to an external directory. Assert every hostile case fails without changing active data.

- [ ] **Step 2: Run and confirm failure**

Run: `node --test scripts/tests/fnos-native-data-lifecycle.test.mjs`

Expected: FAIL until Native data lifecycle actions exist.

- [ ] **Step 3: Implement cold backup protocol**

`upgrade_init` must:

1. Ask Gateway to enter drain mode.
2. Stop Gateway and all Workers.
3. Compute used KiB and require available KiB >= `used * 2 + 102400`.
4. Write `${TRIM_PKGVAR}/backup/sag-users-<UTC timestamp>.tar.gz.tmp`.
5. Validate the archive, chmod 0600, and atomically rename without `.tmp`.
6. Leave the old application stopped for fnOS replacement.
7. On backup failure, preserve active data and best-effort restart the previously running application.

- [ ] **Step 4: Implement restore validation and explicit delete**

Validation opens each `users/<uid>/meta/sag.db` read-only and runs `PRAGMA integrity_check`; every result must be `ok`. Uninstall keeps data unless `SAG_DELETE_DATA=true`. Refuse a users root that is a symlink, non-directory or outside canonical `${TRIM_PKGVAR}`.

- [ ] **Step 5: Run data tests**

Run: `node --test scripts/tests/fnos-native-data-lifecycle.test.mjs scripts/tests/fnos-native-lifecycle.test.mjs`

Expected: PASS; failure fixtures retain byte-identical active data.

- [ ] **Step 6: Commit**

```bash
git add packages/fnos/native/sag/app/runtime/lifecycle.py packages/fnos/native/sag/cmd scripts/tests/fnos-native-data-lifecycle.test.mjs
git commit -m "feat(fnos): protect native user data lifecycle"
```

---

### Task 11: Reproducible x86 Vendor Build and Size Gate

**Files:**
- Create: `scripts/build-fnos-native-vendor.mjs`
- Create: `scripts/build-fnos-native-package.mjs`
- Create: `scripts/fnos-native-size-report.mjs`
- Create: `scripts/tests/fnos-native-vendor.test.mjs`
- Create: `scripts/tests/fnos-native-size.test.mjs`
- Modify: `scripts/tests/fnos-native-package.test.mjs`

**Interfaces:**
- `build-fnos-native-vendor.mjs --platform linux/amd64|linux/arm64 --output <empty-dir>`.
- `build-fnos-native-package.mjs --platform x86|arm --vendor <dir> --web <dir> --version <version> --output <fpk>`.
- `fnos-native-size-report.mjs --platform x86|arm --fpk <file> --rendered <dir> --output <json>`.

- [ ] **Step 1: Write command and safety tests**

Use fake `uv` and `fnpack` executables. Assert the vendor builder invokes locked production export, Python 3.12, the requested Linux platform, `--only-binary :all:`, and an empty destination. Assert it rejects symlink destinations, macOS wheels, Windows DLLs, `.pyc`, `__pycache__`, test directories and unresolved platform tokens.

- [ ] **Step 2: Run and confirm failure**

Run: `node --test scripts/tests/fnos-native-vendor.test.mjs scripts/tests/fnos-native-size.test.mjs`

Expected: FAIL because build and size scripts do not exist.

- [ ] **Step 3: Implement locked vendor generation**

The underlying commands must be equivalent to:

```bash
uv export --frozen --no-dev --no-hashes --no-emit-project --output-file requirements.txt
uv pip install --target vendor --python-version 3.12 --python-platform x86_64-unknown-linux-gnu --only-binary :all: -r requirements.txt
```

Use `aarch64-unknown-linux-gnu` for ARM. Copy `sag_api/` and `sag_agent/` separately into `app/server`; do not install the local project into vendor. Preserve `.dist-info` licenses and metadata. Write a sorted manifest of relative path, byte size and SHA-256.

- [ ] **Step 4: Implement final package render**

Copy the selected vendor, API source, `.next/standalone`, `.next/static` and `public` into a new OS temporary directory. Refuse any source symlink. Replace each manifest token exactly once. Run the Native validator before and after `fnpack build`. Never render into the repository tree.

- [ ] **Step 5: Implement size gates**

Fail when final FPK exceeds 298844160 bytes for x86 or 272629760 bytes for ARM. Fail when unpacked application content exceeds 975175680 bytes for x86 or 901775360 bytes for ARM. Emit exact byte values plus top 20 paths sorted by descending size.

- [ ] **Step 6: Run script tests and one structural render**

Run:

```bash
node --test scripts/tests/fnos-native-vendor.test.mjs scripts/tests/fnos-native-size.test.mjs scripts/tests/fnos-native-package.test.mjs
node scripts/build-fnos-native-package.mjs --structural-test --platform x86 --version 1.6.0-fnos.1 --output /tmp/sag-native-structural-x86.fpk
```

Expected: all tests PASS; structural FPK and JSON size report are created outside the repository.

- [ ] **Step 7: Commit**

```bash
git add scripts/build-fnos-native-vendor.mjs scripts/build-fnos-native-package.mjs scripts/fnos-native-size-report.mjs scripts/tests/fnos-native-*.test.mjs
git commit -m "build(fnos): produce bounded native packages"
```

---

### Task 12: Native Candidate and Release Workflow

**Files:**
- Modify: `.github/workflows/fnos-release.yml`
- Modify: `scripts/tests/fnos-dispatch-workflow.test.mjs`
- Modify: `scripts/tests/fnos-release-workflow.test.mjs`
- Create: `scripts/tests/fnos-native-release-workflow.test.mjs`
- Modify: `docs/fnos/docker-to-native-evaluation.md`

**Interfaces:**
- Produces x86 candidate artifact: `sag-<version>-x86.fpk`, matching `.sha256`, `.size.json`, SBOM and vendor manifest. ARM artifacts are deferred until ARM P0 is scheduled.
- Candidate retention remains one day; publish requires the existing explicit `PUBLISH` confirmation.

- [ ] **Step 1: Write workflow policy tests**

Assert the workflow:

- uses the x86/ubuntu-24.04 release target; ARM/ubuntu-24.04-arm is deferred until ARM P0 is scheduled;
- runs backend/frontend/static fnOS tests before packaging;
- builds vendor and Web on the matching architecture;
- runs size gates before upload;
- does not invoke Docker build, Docker login, image promotion or gateway image scanning in Native jobs;
- uploads the x86 artifact in candidate mode;
- publishes only artifacts from the same run and checked revision.

- [ ] **Step 2: Run and confirm failure**

Run: `node --test scripts/tests/fnos-native-release-workflow.test.mjs scripts/tests/fnos-release-workflow.test.mjs`

Expected: FAIL because the workflow still builds Docker images.

- [ ] **Step 3: Add Native jobs without deleting rollback code**

Replace the user-facing candidate and publish artifact path with Native jobs. Keep old Docker scripts and templates in the repository for one release cycle, but remove them from active workflow triggers and jobs. Cache npm and uv downloads; never cache the final vendor directory across revisions.

- [ ] **Step 4: Add artifact provenance**

Each size report and release manifest must record commit SHA, SAG version, architecture, fnpack version, Python runtime requirement, Node runtime requirement, requirements SHA-256, package SHA-256 and P0 evidence commit.

- [ ] **Step 5: Run workflow tests**

Run:

```bash
node --test scripts/tests/fnos-native-release-workflow.test.mjs scripts/tests/fnos-dispatch-workflow.test.mjs scripts/tests/fnos-release-workflow.test.mjs
```

Expected: PASS; only one user-facing fnOS delivery workflow remains.

- [ ] **Step 6: Commit**

```bash
git add .github/workflows/fnos-release.yml scripts/tests/fnos-*.test.mjs docs/fnos/docker-to-native-evaluation.md
git commit -m "ci(fnos): deliver native multiarch packages"
```

---

### Task 13: x86 Full Verification, Device Acceptance, and Delivery Switch

**Files:**
- Modify: `README-CN.md`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `docs/fnos/multi-user-data-isolation-design.md`
- Modify: `docs/fnos/docker-to-native-evaluation.md`
- Create: `docs/fnos/native-acceptance-checklist.md`

**Interfaces:**
- Produces: a signed-off x86 acceptance checklist with install, start, multi-user, upgrade, backup, uninstall, memory and size results. ARM acceptance remains a deferred checklist item.
- Produces: final documentation that describes Native delivery and no longer tells fnOS users to pull Docker images.

- [ ] **Step 1: Run all automated verification**

```bash
cd apps/api
uv run --extra dev ruff check sag_api sag_agent tests
uv run --extra dev pytest -q

cd ../web
npm run typecheck
npm run lint
npm run test:unit
NEXT_PUBLIC_APP_BASE_PATH=/app/sag NEXT_PUBLIC_API_BASE=/app/sag npm run build

cd ../..
node --test scripts/tests/fnos-*.test.mjs
git diff --check
```

Expected: every command exits 0; no test is newly skipped to make the suite pass.

- [ ] **Step 2: Run x86 device acceptance**

On each device verify:

1. Clean install automatically installs declared runtimes and starts without internet dependency downloads.
2. `/app/sag` opens under HTTP and HTTPS fnOS access domains.
3. Two normal users and one administrator each see an initially empty, independent knowledge base.
4. Cross-user guessed IDs return 404 for sources, documents, Agents, threads and attachments.
5. A 25 MiB upload, document parsing, search SSE and chat SSE complete.
6. Four active users remain usable; a fifth receives bounded 503 behavior when no Worker is reclaimable.
7. Record Gateway, Next and each Worker PSS after idle, search and parse workloads.
8. Upgrade creates and validates a cold archive before replacement.
9. Default uninstall retains data; explicit delete removes only canonical app data.
10. Final FPK stays below the architecture threshold.

- [ ] **Step 3: Apply the Worker memory gate**

Default `max_workers=4` is accepted only if four idle Workers plus Gateway and Next consume at most 70% of RAM on the smallest supported device. Otherwise set the shipped default to the largest value from 1–3 that stays at or below 70%, update both design documents and add the measured value to the checklist.

- [ ] **Step 4: Update user-facing documentation**

Document Native installation, fnOS login integration, per-user private workspaces, retained-data behavior, supported architectures and package sizes. Do not document Docker image variables as the current fnOS installation path.

- [ ] **Step 5: Commit final acceptance**

```bash
git add README-CN.md README.md CHANGELOG.md docs/fnos
git commit -m "docs(fnos): complete native acceptance guide"
```

- [ ] **Step 6: Final branch check**

Run:

```bash
git status --short --branch
git log --oneline origin/fnos/develop..HEAD
```

Expected: clean worktree and one intentional commit per completed task. Push or create a PR for the x86 release only after the x86 acceptance checklist is signed off; ARM remains deferred.

---

## Plan Self-Review Checklist

Before an implementation Agent starts a task, it must verify:

- [ ] The preceding task commit exists in the current branch.
- [ ] Task 2 evidence is passing before Task 3 or later starts.
- [ ] Every consumed interface is produced by an earlier task with the same name and signature.
- [ ] The task changes only its listed files unless a failing test proves an additional in-scope file is required.
- [ ] The failing test was observed before implementation.
- [ ] The focused tests and listed regression tests pass before commit.
- [ ] No secret, vendor directory, FPK, device data, temporary Socket or PID file is committed.
- [ ] Package size and memory claims use generated evidence rather than estimates from the design documents.
