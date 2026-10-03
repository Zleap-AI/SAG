"""MinerU 结果包 sidecar：图片、content_list / middle_json、标题层级与页码。"""

from __future__ import annotations

import io
import json
import os
import zipfile
from typing import Any

import pytest

from sag_api.core.config import Settings
from sag_api.parsing import service
from sag_api.parsing.mineru import _markdown_from_zip
from sag_api.parsing.mineru_artifacts import (
    MinerUResult,
    assets_dir_for,
    result_from_zip,
    rewrite_image_links,
)

PNG = b"\x89PNG\r\n\x1a\nfake-image"


def _zip(entries: dict[str, bytes | str]) -> bytes:
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w") as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return target.getvalue()


def _v1_middle_json() -> str:
    # 结构取自 MinerU 4.0 自部署服务（flash tier）真实返回的 middle_json.json。
    def title(kind: str, text: str, level: int, index: int) -> dict[str, Any]:
        return {
            "type": kind,
            "index": index,
            "content": [{"type": "text", "content": text}],
            "level": level,
        }

    return json.dumps(
        {
            "schema": "docvortex.middle",
            "pages": [
                {
                    "page_idx": 0,
                    "blocks": [
                        title("doc_title", "Annual Report 2026", 1, 0),
                        title("paragraph_title", "1 Introduction", 2, 1),
                        {"type": "text", "index": 2, "content": [{"type": "text", "content": "body"}]},
                    ],
                },
                {
                    "page_idx": 1,
                    "blocks": [
                        title("paragraph_title", "2 Results", 2, 0),
                        title("paragraph_title", "2.1 Discussion", 3, 1),
                    ],
                },
            ],
        }
    )


def test_v1_zip_keeps_images_structured_outputs_and_headings():
    result = result_from_zip(
        _zip(
            {
                "markdown.md": "# Annual Report 2026\n\n![](images/page_1_image_1.jpg)\n",
                "middle_json.json": _v1_middle_json(),
                "structured_content.json": "{}",
                "model_output.json": "{}",
                "images/page_1_image_1.jpg": PNG,
            }
        ),
        1024 * 1024,
    )

    assert result.markdown == "# Annual Report 2026\n\n![](images/page_1_image_1.jpg)\n"
    assert set(result.files) == {
        "images/page_1_image_1.jpg",
        "middle.json",
        "structured_content.json",
        "headings.json",
    }
    assert result.files["images/page_1_image_1.jpg"] == PNG
    assert json.loads(result.files["headings.json"]) == [
        {"title": "Annual Report 2026", "level": 1, "page_idx": 0},
        {"title": "1 Introduction", "level": 2, "page_idx": 0},
        {"title": "2 Results", "level": 2, "page_idx": 1},
        {"title": "2.1 Discussion", "level": 3, "page_idx": 1},
    ]


def test_legacy_zip_prefers_content_list_for_heading_levels():
    content_list = [
        {"type": "text", "text": "Paper Title", "text_level": 1, "page_idx": 0},
        {"type": "text", "text": "Plain paragraph", "page_idx": 0},
        {"type": "image", "img_path": "images/a.jpg", "page_idx": 0},
        {"type": "text", "text": "  Method \n details ", "text_level": 2, "page_idx": 3},
        {"type": "text", "text": "bogus level", "text_level": 9, "page_idx": 3},
    ]
    result = result_from_zip(
        _zip(
            {
                "paper/full.md": "# Paper Title\n",
                "paper/images/a.jpg": PNG,
                "paper/paper_content_list.json": json.dumps(content_list),
                "paper/layout.json": json.dumps({"pdf_info": []}),
                "paper/paper_origin.pdf": b"%PDF",
                "other/images/stray.jpg": PNG,
            }
        ),
        1024 * 1024,
    )

    assert set(result.files) == {
        "images/a.jpg",
        "content_list.json",
        "middle.json",
        "headings.json",
    }
    assert json.loads(result.files["headings.json"]) == [
        {"title": "Paper Title", "level": 1, "page_idx": 0},
        {"title": "Method details", "level": 2, "page_idx": 3},
    ]


def test_unsafe_and_oversized_sidecars_are_skipped_without_failing():
    result = result_from_zip(
        _zip(
            {
                "markdown.md": "# Title\n",
                "images/../../escape.jpg": PNG,
                "/images/abs.jpg": PNG,
                "images/sub\\win.jpg": PNG,
                "images/notes.txt": b"not an image",
                "images/big.jpg": b"x" * 4096,
                "images/small.jpg": PNG,
            }
        ),
        2048,
    )

    assert result.markdown == "# Title\n"
    assert set(result.files) == {"images/small.jpg"}


def test_markdown_from_zip_still_returns_markdown_only():
    assert _markdown_from_zip(_zip({"markdown.md": "# Hi", "images/a.png": PNG}), 1024) == "# Hi\n"


def test_rewrite_image_links_only_touches_saved_images():
    markdown = (
        "![](images/a.jpg)\n"
        '![fig](./images/b.png "caption")\n'
        "![](images/missing.jpg)\n"
        "![](https://example.com/images/a.jpg)\n"
        '<table><tr><td><img src="images/a.jpg" alt="x"></td></tr></table>\n'
    )
    files = {"images/a.jpg": PNG, "images/b.png": PNG}

    rewritten = rewrite_image_links(markdown, "doc.pdf.parsed.sig.assets", files)

    assert rewritten == (
        "![](doc.pdf.parsed.sig.assets/images/a.jpg)\n"
        '![fig](doc.pdf.parsed.sig.assets/images/b.png "caption")\n'
        "![](images/missing.jpg)\n"
        "![](https://example.com/images/a.jpg)\n"
        '<table><tr><td><img src="doc.pdf.parsed.sig.assets/images/a.jpg" alt="x"></td></tr></table>\n'
    )


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=str(tmp_path / "engine"),
        upload_dir=str(tmp_path),
        document_parser="mineru",
        mineru_base_url="https://api.302.ai",
        mineru_api_key="sk-mineru",
    )


@pytest.mark.asyncio
async def test_prepare_document_writes_sidecars_and_resolvable_image_links(tmp_path, monkeypatch):
    calls: list[str] = []

    class FakeMinerU:
        def __init__(self, settings: Settings) -> None:
            pass

        async def parse_result(self, path: str, **_: Any) -> MinerUResult:
            calls.append(path)
            return MinerUResult(
                markdown="# Report\n\n![](images/p0.jpg)\n",
                files={"images/p0.jpg": PNG, "headings.json": b"[]"},
            )

    monkeypatch.setattr(service, "MinerUClient", FakeMinerU)
    source = tmp_path / "doc 1.pdf"
    source.write_bytes(b"%PDF")
    settings = _settings(tmp_path)

    prepared = await service.prepare_document(str(source), settings)

    assets = assets_dir_for(prepared.path)
    assert prepared.provider == "mineru"
    assert os.path.basename(assets) == "doc 1.pdf.parsed.mineru-2.5-auto.assets"
    with open(prepared.path, encoding="utf-8") as cached:
        markdown = cached.read()
    assert markdown == "# Report\n\n![](doc%201.pdf.parsed.mineru-2.5-auto.assets/images/p0.jpg)\n"
    link = markdown.split("](")[1].split(")")[0].replace("%20", " ")
    with open(os.path.join(os.path.dirname(prepared.path), link), "rb") as image:
        assert image.read() == PNG
    assert os.path.isfile(os.path.join(assets, "headings.json"))
    assert not [name for name in os.listdir(tmp_path) if name.startswith(".parsed-")]

    again = await service.prepare_document(str(source), settings)
    assert again.cached is True
    assert calls == [str(source)]

    service.remove_parsed_sidecars(str(source))
    assert not [name for name in os.listdir(tmp_path) if name.startswith("doc 1.pdf")]


@pytest.mark.asyncio
async def test_prepare_document_without_sidecars_keeps_plain_markdown(tmp_path, monkeypatch):
    class FakeMinerU:
        def __init__(self, settings: Settings) -> None:
            pass

        async def parse_result(self, path: str, **_: Any) -> MinerUResult:
            return MinerUResult(markdown="# Plain\n")

    monkeypatch.setattr(service, "MinerUClient", FakeMinerU)
    source = tmp_path / "plain.pdf"
    source.write_bytes(b"%PDF")

    prepared = await service.prepare_document(str(source), _settings(tmp_path))

    assert open(prepared.path, encoding="utf-8").read() == "# Plain\n"
    assert not os.path.exists(assets_dir_for(prepared.path))
