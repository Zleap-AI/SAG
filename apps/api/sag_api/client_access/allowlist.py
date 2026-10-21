"""Explicit method/path rules shared by public listener and final worker boundary."""

import re

_ID = r"[A-Za-z0-9_-]+"
_SEGMENT = r"[^/%\\\x00-\x20]+"
_RULES = [
    ({"GET", "POST"}, r"/sources"),
    ({"GET"}, r"/sources/connectors"),
    ({"GET", "PATCH", "DELETE"}, rf"/sources/{_ID}"),
    ({"POST"}, rf"/sources/{_ID}/sync"),
    ({"GET"}, rf"/sources/{_ID}/chunks/{_ID}"),
    ({"GET", "POST"}, rf"/sources/{_ID}/documents"),
    ({"POST"}, rf"/sources/{_ID}/documents/ingest"),
    ({"GET", "DELETE"}, rf"/sources/{_ID}/documents/{_ID}"),
    ({"GET"}, rf"/sources/{_ID}/documents/{_ID}/(?:file|preview|parsed|read)"),
    ({"POST"}, rf"/sources/{_ID}/documents/{_ID}/(?:reprocess|pause|resume)"),
    ({"GET"}, rf"/sources/{_ID}/(?:outline|grep)"),
    ({"GET"}, rf"/sources/{_ID}/entities/{_SEGMENT}/context"),
    ({"POST"}, rf"/sources/{_ID}/search"),
    ({"POST"}, r"/search"),
    ({"GET"}, rf"/jobs/{_ID}"),
]


def safe_path(path: str, raw_path: bytes | None = None) -> bool:
    return not (
        "%" in path
        or "\\" in path
        or "//" in path
        or any(part in {".", ".."} for part in path.split("/"))
        or any(ord(c) < 32 for c in path)
        or (raw_path is not None and re.search(rb"%(?:2f|5c|2e|25|00)", raw_path, re.I))
    )


def business_allowed(method: str, path: str, raw_path: bytes | None = None) -> bool:
    if not safe_path(path, raw_path):
        return False
    return any(method in methods and re.fullmatch(pattern, path) for methods, pattern in _RULES)


def client_allowed(method: str, path: str, raw_path: bytes | None = None) -> bool:
    if not safe_path(path, raw_path):
        return False
    if path.startswith("/client/v1/api"):
        return business_allowed(method, path.removeprefix("/client/v1/api"), raw_path)
    return (method, path) in {
        ("GET", "/client/v1/info"),
        ("POST", "/client/v1/device/code"),
        ("POST", "/client/v1/token"),
        ("GET", "/client/v1/session"),
        ("POST", "/client/v1/session/revoke"),
    } or (method == "GET" and re.fullmatch(r"/client/v1/operations/[A-Za-z0-9_.:-]{8,200}", path) is not None)


def is_write(method: str, path: str) -> bool:
    return method in {"POST", "PUT", "PATCH", "DELETE"} and not path.endswith("/search")
