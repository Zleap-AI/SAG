# fnOS 手动 FD 采样

`scripts/fnos-fd-sample.py` 只使用 Python 标准库，手动读取一个指定 PID 的 Linux `/proc`，向标准输出写 JSONL。默认采 5 次、间隔 10 秒，约 40 秒结束；不会安装服务、改变进程限制或启动定时任务。

将脚本复制到设备的诊断目录，以能读取目标进程 `/proc` 的同一账户执行。SAG 服务运行账户为 `sag`。gateway 和 MCP proxy 的 PID 分别记在包数据目录的 `run/gateway.pid`、`run/mcp-public.pid`；worker 是 gateway 的子进程，需先用已有进程管理工具确认 PID。每次只采一个进程，分别保存文件。

```sh
# 仓库中运行；把 12345 替换为已确认的目标 PID
python3 scripts/fnos-fd-sample.py 12345 --count 5 --interval 10 > fd-worker.jsonl

# 设备上使用 fnOS 的 Python 3.12；361 次、间隔 10 秒，约一小时结束
/var/apps/python312/target/bin/python3 /tmp/fnos-fd-sample.py 12345 \
  --count 361 --interval 10 > fd-worker.jsonl
```

`PID`、`--count` 必须为正整数，`--interval` 必须为正的有限秒数。最后一次输出后立即退出，按 Ctrl+C 可提前结束。stdout 每行是一条 JSON，示例数值如下。

```json
{"timestamp":"2026-10-17T08:00:00+00:00","sample":1,"pid":12345,"start_time_ticks":9876,"total":107,"counts":{"socket":10,"pipe":4,"anon_inode":6,"sqlite":12,"lancedb":64,"regular":8,"other":3},"nofile":{"soft":4096,"hard":4096},"unresolved":{"vanished":0,"permission_denied":0},"status":"ok"}
```

- `total` 是本次枚举到的 FD 数，各分类相加等于它。采样期间进程仍会打开、关闭 FD，因此不是原子快照。
- `socket`、`pipe`、`anon_inode` 按内核目标类型分类；`sqlite` 按 `.db`、`.sqlite`、`.sqlite3` 及其 WAL/SHM/journal 后缀识别；`lancedb` 按路径中 `.lance` 目录识别。文件分类是定位线索，不能据此确认资源由哪个库持有。
- `regular` 是其余普通文件；`other` 包含其他类型和无法完成分类的条目。`unresolved.vanished` 表示枚举后消失的 FD，`permission_denied` 表示单个条目不可读。工具不会输出文件路径、socket 编号、连接地址、进程名、命令行或环境变量。
- `nofile.soft` 是实际生效上限，`hard` 是当前进程可用的上限；内核的 `unlimited` 保留为字符串。每个 PID 单独比较，不能把多个进程的 FD 总和与某一个进程的 soft 比较。
- `pid` 加 `start_time_ticks` 标识本次目标进程。每次采样前后都会复核；发现 PID 已被新进程复用时，输出 `process_replaced` 并停止，重新确认 PID 后再运行。

正常结束返回 0；进程消失、权限不足、PID 被复用或 `/proc` 无法读取时输出相应状态并返回 1。参数错误返回 2，Ctrl+C 返回 130。错误状态行没有有效的 FD 快照。`permission_denied` 时先确认运行账户和目标 PID，不要通过修改系统 `/proc` 权限来绕过。

复现时保留相同的模型、文档规模和抽取并发，记录设备、fnOS/SAG 版本以及任务开始、结束时间。观察预热后、任务进行中和任务结束后的趋势：连接池、数据库缓存可能增长后稳定在平台；持续上升且空闲后仍不回落需要结合任务量、分类和资源关闭路径继续定位。单次高值或数次上涨都不能直接证明泄漏。工具只提供原始计数，不自动判定泄漏。
