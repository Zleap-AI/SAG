"""真实解析样本生成器（测试专用，零外部资源）。

样本全部在测试运行时合成，不依赖企业资料或网络。集中放在这里是为了让
解析服务、AnyDoc 适配与表格入库三组测试共用同一批样本，避免各写一份。

生成的样本：

- ``write_docx``：含中文标题、正文与悬空图片关系的 DOCX（沿用既有 helper 语义）
- ``write_pptx``：含中文标题的 PPTX（需要 python-pptx，缺失时跳过）
- ``write_xlsx``：含中文表头与数字的 XLSX（需要 openpyxl，缺失时跳过）
- ``write_text_pdf``：带可提取文本层的最小 PDF
- ``write_scanned_pdf``：只有一张图片、无文本层的 PDF（纯扫描件）
- ``write_mixed_pdf``：第 1 页有文本、第 2 页只有图片的混合 PDF
- ``write_epub``：最小合法 EPUB（ZIP + mimetype + container.xml + XHTML）
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

_DOCX_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml"
    ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""

_DOCX_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
    Target="word/document.xml"/>
</Relationships>"""


def write_docx(path: Path, title: str, body: str) -> None:
    """生成含标题段与正文段的最小 DOCX。"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", _DOCX_CONTENT_TYPES)
        archive.writestr("_rels/.rels", _DOCX_ROOT_RELS)
        archive.writestr(
            "word/document.xml",
            f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
            <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
              <w:body>
                <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
                  <w:r><w:t>{title}</w:t></w:r></w:p>
                <w:p><w:r><w:t>{body}</w:t></w:r></w:p>
              </w:body>
            </w:document>""",
        )


def write_pptx(path: Path, title: str) -> bool:
    """生成含中文标题的单页 PPTX；python-pptx 不可用时返回 False。"""
    try:
        from pptx import Presentation
    except ImportError:
        return False
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[5])
    slide.shapes.title.text = title
    presentation.save(path)
    return True


def write_xlsx(path: Path, rows: list[tuple[object, ...]], sheet_title: str = "Sheet1") -> bool:
    """生成 XLSX；openpyxl 不可用时返回 False。"""
    try:
        from openpyxl import Workbook
    except ImportError:
        return False
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = sheet_title
    for row in rows:
        sheet.append(list(row))
    workbook.save(path)
    return True


def write_epub(path: Path, title: str, body: str) -> None:
    """生成最小合法 EPUB（mimetype 必须是第一个且不压缩的条目）。"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            zipfile.ZipInfo("mimetype"),
            "application/epub+zip",
            compress_type=zipfile.ZIP_STORED,
        )
        archive.writestr(
            "META-INF/container.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>""",
        )
        archive.writestr(
            "OEBPS/content.opf",
            f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">urn:uuid:sag-test</dc:identifier>
    <dc:title>{title}</dc:title>
    <dc:language>zh</dc:language>
  </metadata>
  <manifest>
    <item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="chapter"/></spine>
</package>""",
        )
        archive.writestr(
            "OEBPS/chapter.xhtml",
            f"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>{title}</title></head>
<body><h1>{title}</h1><p>{body}</p></body></html>""",
        )


def write_text_pdf(path: Path, text: str) -> None:
    """写一个带可提取文本层的最小 PDF。"""
    path.write_bytes(_pdf(_text_page(text)))


def write_scanned_pdf(path: Path) -> None:
    """写一个无文本层、只有一张图片的 PDF（模拟纯扫描件）。"""
    path.write_bytes(_pdf(_image_page()))


def write_mixed_pdf(path: Path, text: str) -> None:
    """第 1 页有文本层、第 2 页只有图片的混合 PDF。"""
    path.write_bytes(_pdf(_text_page(text), _image_page()))


# ── PDF 内部构造 ────────────────────────────────────────────────────
_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300ffffffffffffffffff"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
    "ffffffffffffffffffffffffffffffffffffc0001108000100010301220002110103"
    "1101ffc4001f0000010501010101010100000000000000000102030405060708090a"
    "0bffc400b5100002010303020403050504040000017d010203000411051221314106"
    "13516107227114328191a1082342b1c11552d1f02433627282090a161718191a2526"
    "2728292a3435363738393a434445464748494a535455565758595a63646566676869"
    "6a737475767778797a838485868788898a92939495969798999aa2a3a4a5a6a7a8a9"
    "aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5"
    "e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffda0008010100003f00fbfeffd9"
)
_IMAGE_CONTENT = b"q 200 0 0 200 100 500 cm /Im0 Do Q"


def _text_page(text: str) -> dict[str, bytes]:
    stream = f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET".encode()
    return {
        "kind": "text",
        "content": stream,
        "resources": b"<< /Font << /F1 /FONT >> >>",
    }


def _image_page() -> dict[str, bytes]:
    return {
        "kind": "image",
        "content": _IMAGE_CONTENT,
        "resources": b"<< /XObject << /Im0 /IMAGE >> >>",
    }


def _pdf(*pages: dict[str, bytes]) -> bytes:
    """按对象编号组装 PDF：1=Catalog、2=Pages、3=Font、4=Image，随后每页 2 个对象。"""
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids ["
        + b" ".join(f"{5 + index * 2} 0 R".encode() for index in range(len(pages)))
        + b"] /Count "
        + str(len(pages)).encode()
        + b" >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 "
        b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length "
        + str(len(_JPEG)).encode()
        + b" >>\nstream\n"
        + _JPEG
        + b"\nendstream",
    ]
    for page in pages:
        resources = page["resources"]
        for name, reference in ((b"/FONT", b"3 0 R"), (b"/IMAGE", b"4 0 R")):
            resources = resources.replace(name, reference)
        content = page["content"]
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources "
            + resources
            + b" /Contents "
            + f"{len(objects) + 2} 0 R".encode()
            + b" >>"
        )
        objects.append(
            b"<< /Length "
            + str(len(content)).encode()
            + b" >>\nstream\n"
            + content
            + b"\nendstream"
        )

    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(output)


def legacy_xls_bytes(*rows: tuple[object, ...]) -> bytes | None:
    """用 xlwt 生成 BIFF8 旧版 .xls；依赖缺失时返回 None。"""
    try:
        import xlwt
    except ImportError:
        return None
    book = xlwt.Workbook()
    sheet = book.add_sheet("成本")
    for row_index, row in enumerate(rows):
        for column_index, value in enumerate(row):
            sheet.write(row_index, column_index, value)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()
