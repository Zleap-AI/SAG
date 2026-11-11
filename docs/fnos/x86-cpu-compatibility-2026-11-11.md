# fnOS x86 CPU 兼容与初始化恢复验收

日期：2026-11-11（Asia/Shanghai）。范围：fnOS Native x86；SDK 保持 `zleap-sag==0.15.0`。

## 故障链与包内实物

用户报告的非法指令位于 LanceDB 原生扩展。现有 `1.5.13-fnos` 实机安装目录的
`lancedb/_lancedb.abi3.so` SHA-256 为
`7bef83b94bef1b2b39432ade0a7108b834a1c3dd03388ff1fdf502cd51ac6f07`，
与从当前 frozen lock 导出构建的 Linux x86 vendor 完全一致，分发版本是 `lancedb==0.34.0`。

[官方 Python README](https://github.com/lancedb/lancedb/blob/main/python/README.md)
说明标准 x86 wheel 采用 Haswell 基线，需要 AVX2、FMA、F16C；
[官方问题 #3324](https://github.com/lancedb/lancedb/issues/3324) 记录了旧 CPU 的 SIGILL。
本轮以同一份 0.34.0 vendor 在 QEMU Nehalem 下执行导入，进程退出码为 132，
输出 `uncaught target signal 4 (Illegal instruction)`。这与用户证据一致。

SDK 在第一次关系数据库连接之前先调用 `ensure_local_dirs()` 创建 `lancedb/`，
随后加载向量原生库。SIGILL 会终止整个 Worker，不能被 Python 异常处理恢复；
因此可能留下没有引擎 `sag.db` 的空目录。旧目录判定又阻止下一次启动，最终表现为
`/auth/me` 返回初始化失败 503。空目录是崩溃残留，CPU 指令不兼容才是本次首次故障原因。

## 依赖方案

官方 [lancedb-compat](https://pypi.org/project/lancedb-compat/) 使用 x86-64-v2 基线和运行时 SIMD 分派。
它与标准分发占用相同导入命名空间，不能混装。

- PyPI 没有 `lancedb-compat==0.34.0`。
- 实物检查发现 0.36.0、0.37.1、0.38.0、0.40.0 的 Python 入口仍查询标准分发的版本元数据，
  单独安装不能正常导入；问题见 [#3950](https://github.com/lancedb/lancedb/issues/3950)。
- 0.41.0 官方完整 wheel 已从原生模块读取版本，包含 [#3973](https://github.com/lancedb/lancedb/pull/3973)
  所述修复；本次不修改它的 Python 源码、原生 `.so` 或分发元数据。

x86 vendor 精确固定 `lancedb-compat==0.41.0` 及其要求的 namespace/client 0.13.0，
官方完整 wheel URL 和 SHA 记录在 `scripts/fnos-x86-vendor-requirements.txt`。
其余依赖保留 `apps/api/uv.lock` 的版本和哈希。导出时排除被替代的三个标准分发，
以 `--no-deps --require-hashes` 一次安装到空目录，避免 SDK 重新拉入标准 LanceDB。
构建后按 Python 3.12/Linux/x86 marker 核验完整依赖闭包；仅 SDK 的 `lancedb>=0.16`
要求允许由官方 compat 分发满足，并检查版本范围。不伪造 `lancedb.dist-info`。
ARM 构建行为与主锁文件均未改变。

## 无 AVX 环境验证

宿主为 ARM64 Docker VM；在 ARM Debian 容器内显式使用 `qemu-x86_64 7.2.22 -cpu Nehalem`，
运行 Linux x86 CPython 3.12.16。测试读取 CPUID 指令本身，不使用会反映宿主的 `/proc/cpuinfo`。

```json
{"avx":false,"avx2":false,"f16c":false,"fma":false,"popcnt":true,"sse3":true,"sse4_1":true,"sse4_2":true,"ssse3":true}
```

已验证：

| 场景 | 结果 |
| --- | --- |
| 标准 0.34.0 / Nehalem 导入负例 | SIGILL，退出 132 |
| 官方未修改 compat 0.41.0 / Nehalem | 导入、建表、文本与向量写入、向量检索通过 |
| SDK 0.15 + 应用 EngineManager / Nehalem | 初始化、分块、关系与向量入库、应用批量向量检索通过，检索到真实文档内容 |
| 独立新进程重新打开 compat 创建的数据 / Nehalem | 初始化与相同文档检索通过 |
| 标准 0.34.0 / Haswell 创建 SDK 数据 | 文档入库与应用检索通过 |
| compat 0.41.0 / Nehalem 打开上述 0.34.0 数据 | 原有文档检索通过 |

测试脚本为 `scripts/fnos-native-cpu-smoke.py`，使用隔离数据目录和确定性的本地 embedding HTTP fixture，
不发送真实资料或密钥到模型平台。每次 write/reopen 都检查检索结果中确实包含文档内容，
不是仅检查接口返回成功。模型质量、商业服务密钥与计费不属于该 fixture 的验证范围。

发布工作流在打包之前使用 QEMU 执行 write/reopen；脚本要求 CPUID 中 AVX/AVX2/FMA/F16C 全部关闭，
普通 CPU 上通过不会被视作旧 CPU 验收。新基线仍要求 x86-64-v2；不宣称兼容低于该基线的 CPU。

## 初始化中断恢复保护

仅复用已识别的空 `lancedb/` 目录，不删除文件、不清空数据库。
无已登记文档时保留原有 marker 和旧引擎格式检查；已有首次上传文档时，必须同时确认：
初始化标记已提交、文档和信源没有索引成果、原始上传文件路径安全且大小匹配、
有可重放的 PROCESS_DOCUMENT QUEUED/RUNNING 任务且没有索引 checkpoint。
已有成功文档、丢失原文件、未知文件、非空索引、符号链接和损坏数据库仍会拒绝自动恢复。

测试覆盖 SDK 自己创建目录后被中断、恢复检查、实际 SDK 后续建库，以及任务队列把 RUNNING
恢复为 QUEUED 并重新入队。旧版生命周期脚本可能使用系统 Python，因此独立判定器保留 Python 3.9 可执行性。

## 交付边界

完整本地验证：后端 1075 passed / 5 skipped，前端 554 passed，i18n/typecheck/lint/build 通过；
`SAG_FNPACK_TESTS=1` 的全部 fnOS 合约 196 passed / 14 skipped（Linux `/proc` 专属项在 macOS 跳过）。
源码 lint 和 diff 检查通过。此前发现的独立脚本 Python 3.9 兼容问题已修正并完成整套复跑。

实机隔离验收：FN-EVO4-9C3D，x86_64，内核 `6.18.18.c1032-trim`，使用系统提供的 Python 3.12/Node 22，
以 `sag` 包用户运行实际 FPK 解包内容，持久化目录与服务 socket 均放在独立临时树。
SDK 自己创建残留目录后，实际 `main upgrade-knowledge` 完成冷备且元数据库逐字节不变；
两轮 `main start/stop` 的健康检查、`auth/me`、信源列表和聊天页均返回 200。
实际 `install_callback` 对保留的空目录故障数据也通过，并生成冷备。
这是实机隔离生命周期验收，不是应用中心对现有 SAG 的覆盖安装验收；原有 SAG 安装和知识数据未被替换。

本轮 FPK 使用正式版本格式 `1.5.14-fnos`，由共享版本解析器根据已发布的 fnOS 版本推导。
PR 合并和 GitHub Release 发布是独立动作；本地生成包不等于已通过远端 Delivery 发布。
FPK 大小 302,813,340 字节，SHA-256：
`6e38809d2bc23667087a9f5f8f3707674c47d550330ab70a0da7ca0949b1758d`。
包内判定器与修复源码一致，原生库哈希与官方 compat wheel 一致，120 个 vendor 分发版本与 QEMU 验证环境一致。

故障用户建议在应用中心停止 SAG，卸载时明确选择保留数据，再手动安装此 FPK。
新包的保留数据安装检查已在隔离树验证；不要选择删除应用数据，也不要自行清空索引目录。
不宣称旧安装脚本一定允许直接覆盖升级：本轮 CLI `install-fpk` 对已安装应用返回 installed，未执行覆盖替换。
无 AVX 模拟验证不能替代用户实际设备安装确认，但能直接验证此次非法指令的修复路径。
