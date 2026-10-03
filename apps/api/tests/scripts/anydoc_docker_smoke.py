"""API 镜像内 AnyDoc 本地转换验收脚本（在容器中执行）。

用法（仓库根目录）：
    docker run --rm --network none -v "<repo>/apps/api/tests:/tmp/tests:ro" \
        -e PYTHONPATH=/tmp sag-api-anydoc-final:local python /tmp/tests/scripts/anydoc_docker_smoke.py

脚本使用真实 AnyDoc 转换合成样本，断言正文、中文与表格内容，并确认：
- 原生扩展可加载、版本锁定；
- 显式 ``ocr="reject"`` 下存在 Firecrawl 环境变量也不会请求托管接口；
- 扫描 PDF 整份提示 OCR 且带页码。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sag_api.core.errors import ApiError  # noqa: E402
from sag_api.parsing import anydoc as anydoc_parser  # noqa: E402
from tests.helpers import corpus  # noqa: E402


async def check_spreadsheet(work: Path) -> None:
    import hashlib

    from zleap.sag.pipeline import ChunkOptions, DocumentSource, FileSource
    from zleap.sag.pipeline.chunk import ChunkStage
    from zleap.sag.pipeline.parse import ParseStage

    from sag_api.core.config import Settings
    from sag_api.parsing import prepare_document

    original = work / "costs.xls"
    payload = (Path(__file__).resolve().parents[1] / "fixtures" / "costs-general.xls").read_bytes()
    original.write_bytes(payload)
    prepared = await prepare_document(str(original), Settings(_env_file=None, document_parser="anydoc"))
    assert prepared.provider == "markitdown"
    source = DocumentSource(
        content=Path(prepared.path).read_text(encoding="utf-8"),
        original_file=FileSource(path=str(original)),
        original_sha256=hashlib.sha256(payload).hexdigest(),
    )
    parsed = await ParseStage().run(source)
    chunks = await ChunkStage().run(parsed, ChunkOptions(strategy="standard"))
    body = "\n".join(chunk.content for chunk in chunks.chunks)
    assert "| 材料 | 10 |" in body and "| 材料 | 20 |" in body
    assert len({chunk.metadata.get("record_group_id") for chunk in chunks.chunks} - {None}) == 2
    assert original.read_bytes() == payload
    print("[xls] MarkItDown route, numeric evidence and two record groups: OK")


def main() -> int:
    import importlib.metadata

    version = importlib.metadata.version("firecrawl-anydoc")
    print(f"[anydoc] installed version = {version}")
    assert version == "0.2.4", version
    engine_version = importlib.metadata.version("zleap-sag")
    assert engine_version == "0.13.0+sag.1", engine_version
    print(f"[engine] installed hotfix = {engine_version}")

    # 存在 Firecrawl 凭据时也必须走本地 reject 分支，不访问任何托管端点。
    os.environ["FIRECRAWL_API_KEY"] = "fc-should-never-be-used"
    os.environ["FIRECRAWL_API_URL"] = "http://127.0.0.1:1"

    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        docx = work / "报告.docx"
        corpus.write_docx(docx, "季度成本报告", "材料成本同比上升 12%。")
        markdown = anydoc_parser.convert(str(docx))
        print(f"[docx] {len(markdown)} chars")
        assert "季度成本报告" in markdown
        assert "材料成本同比上升 12%。" in markdown

        csv_path = work / "成本.csv"
        original = "姓名,城市,金额\n张三,北京,120\n".encode("gb18030")
        csv_path.write_bytes(original)
        csv_markdown = anydoc_parser.convert(str(csv_path))
        print(f"[csv] {len(csv_markdown)} chars")
        assert "姓名" in csv_markdown and "张三" in csv_markdown and "北京" in csv_markdown
        assert "| --- |" in csv_markdown
        assert csv_path.read_bytes() == original, "上传原文件必须保持不变"

        text_pdf = work / "text.pdf"
        corpus.write_text_pdf(text_pdf, "AnyDoc Docker marker")
        assert "AnyDoc Docker marker" in anydoc_parser.convert(str(text_pdf))
        print("[pdf] text layer extracted")

        scanned = work / "scanned.pdf"
        corpus.write_scanned_pdf(scanned)
        try:
            anydoc_parser.convert(str(scanned))
        except ApiError as error:
            print(f"[scan] {error.message}")
            assert "第 1 页（共 1 页）" in error.message
            assert error.retryable is False
        else:
            raise AssertionError("scanned PDF must not produce markdown")

        import asyncio

        asyncio.run(check_spreadsheet(work))

    print("anydoc docker smoke: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
