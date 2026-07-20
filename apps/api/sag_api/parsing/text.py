"""文本文件编码识别与 UTF-8 规范化。

浏览器 ``Response.text()`` 和 zleap-sag 的 Markdown loader 都以 UTF-8 为边界；
上传文件本身则可能来自 GBK/GB18030、Big5、Shift-JIS 或 Windows-1252。
这里保留原始字节，只为解析与预览生成可靠的 Unicode 文本。
"""

from __future__ import annotations

import codecs
import os
import unicodedata
from dataclasses import dataclass

from charset_normalizer import from_bytes

_PLAIN_TEXT_SUFFIXES = {
    ".txt",
    ".text",
    ".log",
}
_TEXT_PREVIEW_SUFFIXES = _PLAIN_TEXT_SUFFIXES | {
    ".csv",
    ".json",
    ".tsv",
    ".xml",
    ".yaml",
    ".yml",
}
_TEXT_APPLICATION_TYPES = {
    "application/csv",
    "application/json",
    "application/ld+json",
    "application/xml",
    "application/x-ndjson",
    "application/yaml",
}


class TextDecodingError(ValueError):
    """文件字节无法被可靠地识别为文本。"""


@dataclass(frozen=True, slots=True)
class DecodedText:
    text: str
    encoding: str
    replacement_count: int = 0


def is_plain_text_path(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in _PLAIN_TEXT_SUFFIXES


def is_text_preview(filename: str, content_type: str | None) -> bool:
    media_type = (content_type or "").partition(";")[0].strip().lower()
    suffix = os.path.splitext(filename)[1].lower()
    return (
        media_type.startswith("text/")
        or media_type in _TEXT_APPLICATION_TYPES
        or suffix in _TEXT_PREVIEW_SUFFIXES
    )


def read_text_file(path: str) -> DecodedText:
    with open(path, "rb") as source:
        return decode_text_bytes(source.read())


def decode_text_bytes(data: bytes) -> DecodedText:
    if not data:
        return DecodedText("", "utf-8")

    bom_encoding = _bom_encoding(data)
    if bom_encoding:
        return _decode_candidate(data, bom_encoding, strict=True)

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    else:
        _assert_text_quality(text)
        return DecodedText(text, "utf-8")

    # charset-normalizer is a useful general fallback, but it can classify short
    # GBK text as a lossless Western single-byte encoding (classic mojibake).
    # Keep its candidate while first checking for a strong East-Asian signal.
    try:
        match = from_bytes(data).best()
    except (LookupError, UnicodeError):
        match = None

    # Tolerant East-Asian fallback. Choose the candidate with the fewest damaged
    # characters and strongest coherent CJK/Kana signal instead of accepting a
    # technically lossless Latin-1 mojibake decode.
    candidates: list[tuple[float, DecodedText]] = []
    for encoding in ("gb18030", "gbk", "big5", "shift_jis"):
        try:
            decoded = _decode_candidate(data, encoding, strict=False)
        except TextDecodingError:
            continue
        text_length = max(1, len(decoded.text))
        replacement_ratio = decoded.replacement_count / text_length
        east_asian_ratio = _east_asian_count(decoded.text) / text_length
        if east_asian_ratio < 0.05 or decoded.replacement_count > max(
            2, text_length // 100
        ):
            continue
        try:
            _assert_text_quality(decoded.text)
        except TextDecodingError:
            continue
        score = replacement_ratio * 100 - east_asian_ratio
        candidates.append((score, decoded))
    if candidates:
        return min(candidates, key=lambda item: item[0])[1]

    if match is not None and match.encoding:
        text = str(match)
        try:
            _assert_text_quality(text)
        except TextDecodingError:
            pass
        else:
            return DecodedText(text, match.encoding, text.count("�"))

    for encoding in ("cp1252", "latin-1"):
        try:
            decoded = _decode_candidate(data, encoding, strict=False)
        except TextDecodingError:
            continue
        if decoded.replacement_count > max(2, len(decoded.text) // 1000):
            continue
        try:
            _assert_text_quality(decoded.text)
        except TextDecodingError:
            continue
        return decoded

    raise TextDecodingError("无法可靠识别文本编码")


def _bom_encoding(data: bytes) -> str | None:
    # Longest BOM first because UTF-32-LE begins with the UTF-16-LE marker.
    for marker, encoding in (
        (codecs.BOM_UTF32_BE, "utf-32"),
        (codecs.BOM_UTF32_LE, "utf-32"),
        (codecs.BOM_UTF8, "utf-8-sig"),
        (codecs.BOM_UTF16_BE, "utf-16"),
        (codecs.BOM_UTF16_LE, "utf-16"),
    ):
        if data.startswith(marker):
            return encoding
    return None


def _decode_candidate(data: bytes, encoding: str, *, strict: bool) -> DecodedText:
    errors = "strict" if strict else "replace"
    try:
        text = data.decode(encoding, errors=errors)
    except UnicodeDecodeError as error:
        raise TextDecodingError(f"文本不是有效的 {encoding} 编码") from error
    _assert_text_quality(text)
    return DecodedText(text, encoding, text.count("�"))


def _assert_text_quality(text: str) -> None:
    if not text:
        return
    if "\x00" in text:
        raise TextDecodingError("文件包含 NUL 字节，不像文本")
    controls = sum(
        1
        for char in text
        if char not in "\t\n\r" and unicodedata.category(char) in {"Cc", "Cs"}
    )
    if controls > max(2, len(text) // 100):
        raise TextDecodingError("文件包含过多控制字符，不像文本")
    if text.count("�") > max(8, len(text) // 100):
        raise TextDecodingError("文本解码损坏比例过高")


def _east_asian_count(text: str) -> int:
    return sum(
        1
        for char in text
        if "\u3400" <= char <= "\u9fff"
        or "\u3040" <= char <= "\u30ff"
        or "\uac00" <= char <= "\ud7af"
    )
