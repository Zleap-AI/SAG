# zleap-sag General 数值显示热修复

应用固定使用 `zleap-sag==0.13.0+sag.1`。此本地版本以官方 0.13.0 wheel 为基础，
仅修复 `pipeline/spreadsheets/reader.py` 的 General 数字显示：只删除小数部分
多余的尾零，保留整数 10、20、100 的末尾零。没有修改数据契约、数据库模型或原始文档。

- 上游版本：[`zleap-sag 0.13.0`](https://pypi.org/project/zleap-sag/0.13.0/)。
- 上游 wheel SHA-256：`2beee6f89a66af20adda6c3e96321a99113de6f682d2f2a023d6f50297bd41a9`。
- 热修复 wheel SHA-256：`cbeb3c4cae716438d6098eb86518428250596f8838822e0556c83bcaf10a2c45`。
- 许可证：MIT；wheel 中完整保留 Zleap Team 的版权和许可证。
- 构建脚本：`../scripts/build_zleap_hotfix.py`，仅使用 Python 标准库，校验上游哈希、
  修复表达式和版本，重建 wheel RECORD；不会修改已安装的第三方包。

```diff
 if places is None:
-    return format(value, "f").rstrip("0").rstrip(".") or "0"
+    rendered = format(value, "f")
+    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered
```

在 `apps/api` 下复现（先从 PyPI 页面下载上述官方 wheel；输出路径必须尚不存在）：

```bash
python scripts/build_zleap_hotfix.py /path/to/zleap_sag-0.13.0-py3-none-any.whl \
  --output /path/to/zleap_sag-0.13.0+sag.1-py3-none-any.whl
```

`uv sync --frozen` 使用 `pyproject.toml` 的本地 wheel 来源。使用 pip 时先安装
`pip install --no-deps vendor/zleap_sag-0.13.0+sag.1-py3-none-any.whl`，再安装应用；
Docker 与 CI 已使用相同顺序。不要单独用公开 0.13.0 替换此依赖。

待上游发布经过本项目数值回归验证的修复版后，统一更新默认及 PostgreSQL extra
版本、uv.lock、Docker／CI／安装说明，移除此 wheel、构建脚本和本地来源。
曾用旧版生成的分块需要重新处理；本修复不会自动重建历史数据。
