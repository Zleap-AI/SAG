# 桌面发布性能记录

分析日期：2026-09-26。源码基线：`4542e07c66c279edd919f3df346c751259f8948c`。本文记录发布编排和依赖缓存优化；安装格式和签名政策保持不变。同分支的桌面运行管理改动见桌面 README。

## 实测基线

数据来自 GitHub Actions 的 job/step 时间与 macOS job 日志。

| 指标 | [v1.8.10](https://github.com/Zleap-AI/SAG/actions/runs/35707841464) | [v1.8.11](https://github.com/Zleap-AI/SAG/actions/runs/35809234000) |
| --- | ---: | ---: |
| 流水线创建至最后更新 | 22:23 | 24:08 |
| 最慢质量检查（后端测试 job） | 3:49 | 4:20 |
| macOS job | 17:12 | 18:57 |
| macOS 后端依赖安装 | 1:27 | 1:27 |
| macOS 编译与资源准备 | 3:34 | 4:00 |
| macOS 打包、签名、公证 | 10:35 | 12:07 |
| Windows job | 12:13 | 12:36 |
| 最终发布 job | 1:05 | 0:35 |

两次 macOS 日志都显示 `npm cache is not found` 和 `No GitHub Actions cache found`。uv 缓存键相同，仍没有跨版本命中。

签名开始至 `notarization successful` 分别为约 8:52、10:33。现有日志没有签名结束时间，因此不能把整段都归因于 Apple 公证等待。v1.8.11 macOS 产物上传步骤只有 8 秒，上传并非主瓶颈。

## 本轮调整

1. **并行编排。** macOS/Windows 构建仍依赖版本校验，但不再等待完整质量检查。发布 job 显式依赖质量检查、版本校验和两平台构建，默认成功条件保持有效。签名凭据仍只进入凭据检查与最终签名步骤。
2. **在默认分支预热依赖。** 新增 main-only 原生平台预热，共用依赖安装 action，保存 npm/uv 下载缓存。根据 [GitHub 缓存作用域规则](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching#restrictions-for-accessing-a-cache)，标签可以读取默认分支缓存，但不能读取另一个标签的缓存。冷缓存时继续完整安装，不缓存或复用签名后的应用。
3. **拆分构建计时。** 将原有 `prepare:release` 的四条顺序命令展开为独立步骤；保留执行顺序和命令，方便定位下一轮瓶颈。

并行收益模型：以历史 job 时长不变为前提，忽略排队和调度开销。

```text
原先：max(质量检查, 版本校验) + max(macOS, Windows) + 发布
调整：max(质量检查, 版本校验 + max(macOS, Windows)) + 发布
```

对应两次历史记录，预计分别减少 222 秒和 253 秒，约为模型总耗时的 17%。这是调度估算，不是修改后实测。缓存收益未计入，取决于预热完成时间、缓存保留、工具链版本与下载速度。

成本：依赖变化时新增两平台预热任务；质量检查失败时，已开始的原生构建可能继续运行。优化目标是缩短发布等待，不是承诺减少 Actions 总计算用量。

## 验收

- 本地运行 `node --test scripts/tests/*.test.mjs`，覆盖发布门禁、不可变标签、更新通道和发布重试；用 actionlint 检查工作流。
- 合入 main 后检查 Desktop Dependency Cache 两平台均成功，并确认缓存来自 main。
- 使用现有 Desktop Release 的 main 手动验收：确认质量检查与原生构建重叠、依赖缓存恢复、两平台产物校验通过；手动验收不会创建公开 Release。
- 下一次正式版本发布记录相同 job/step 耗时，比较实际总耗时与缓存命中。runner 排队、环境审批和 Apple 公证波动应单独记录。

本地脚本测试不能验证 GitHub 缓存实际命中、原生安装包、macOS 签名公证或优化后的真实耗时。这些需要合入后的受控流水线验收；本轮未触发生产发布。
