"""Compatibility shims for dependency-owned zleap-sag behavior.

These patches live at the application boundary so we can keep user workflows
working while waiting for upstream package releases.
"""

from __future__ import annotations

import copy
from typing import Any

from sag_api.core.logging import get_logger

log = get_logger("sag.compat")


def _without_required_field(node: dict[str, Any], field: str) -> bool:
    required = node.get("required")
    if not isinstance(required, list) or field not in required:
        return False
    node["required"] = [item for item in required if item != field]
    return True


def _looks_like_extract_response_schema(schema: Any) -> bool:
    if not isinstance(schema, dict):
        return False
    data = schema.get("properties", {}).get("data")
    if not isinstance(data, dict):
        return False
    data_props = data.get("properties")
    return (
        schema.get("type") == "object"
        and schema.get("properties", {}).get("type", {}).get("const") == "response"
        and isinstance(data_props, dict)
        and "items" in data_props
        and "meta" in data_props
    )


def _relax_extract_schema(schema: dict[str, Any]) -> dict[str, Any]:
    relaxed = copy.deepcopy(schema)
    data = relaxed.get("properties", {}).get("data")
    if isinstance(data, dict):
        _without_required_field(data, "meta")
        meta = data.get("properties", {}).get("meta")
        if isinstance(meta, dict):
            _without_required_field(meta, "reason")
    event = relaxed.get("definitions", {}).get("event")
    if isinstance(event, dict):
        _without_required_field(event, "is_valid")
    return relaxed


def _repair_extract_response(result: Any) -> set[str]:
    repaired: set[str] = set()
    if not isinstance(result, dict) or result.get("type") != "response":
        return repaired
    data = result.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return repaired
    meta = data.get("meta")
    if not isinstance(meta, dict):
        data["meta"] = {"reason": "model omitted data.meta; filled by SAG compatibility layer"}
        meta = data["meta"]
        repaired.add("data.meta")
    reason = meta.get("reason")
    if not isinstance(reason, str):
        meta["reason"] = ""
        repaired.add("data.meta.reason")

    def repair_item(item: Any) -> None:
        if not isinstance(item, dict):
            return
        if "is_valid" not in item:
            item["is_valid"] = True
            repaired.add("data.items[].is_valid")
        children = item.get("children")
        if isinstance(children, list):
            for child in children:
                repair_item(child)

    for item in data["items"]:
        repair_item(item)
    return repaired


def install_zleap_sag_extract_compat() -> None:
    """Allow event extraction to accept minor omissions in model output.

    Some OpenAI-compatible models produce valid event ``data.items`` but omit
    telemetry-only ``data.meta`` or the boolean ``is_valid`` flag.  Upstream
    zleap-sag validates these fields before its parser can use the extracted
    events, even though the parser already treats missing ``is_valid`` as true.
    We keep strict validation for title/content/references and restore the
    compatible defaults before zleap-sag's own output validator runs.
    """

    from zleap.sag.modules.extract.processor import EventProcessor

    current = EventProcessor._call_llm_with_retry
    if getattr(current, "_sag_api_extract_meta_compat", False):
        return

    async def _patched_call_llm_with_retry(self, messages, schema):  # type: ignore[no-untyped-def]
        active_schema = schema
        if _looks_like_extract_response_schema(schema):
            active_schema = _relax_extract_schema(schema)
        result = await current(self, messages, active_schema)
        repaired = _repair_extract_response(result)
        if repaired:
            log.info("已兼容补齐 zleap-sag 事项抽取响应字段：%s", ", ".join(sorted(repaired)))
        return result

    _patched_call_llm_with_retry._sag_api_extract_meta_compat = True  # type: ignore[attr-defined]
    EventProcessor._call_llm_with_retry = _patched_call_llm_with_retry
