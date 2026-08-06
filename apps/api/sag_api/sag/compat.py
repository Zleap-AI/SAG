"""Compatibility shims for dependency-owned zleap-sag behavior.

These patches live at the application boundary so we can keep user workflows
working while waiting for upstream package releases.
"""

from __future__ import annotations

import copy
from typing import Any

from sag_api.core.logging import get_logger

log = get_logger("sag.compat")





def _llm_model_name(client: Any) -> str:
    current = client
    while current is not None:
        model = getattr(getattr(current, "config", None), "model", None)
        if isinstance(model, str) and model:
            return model
        current = getattr(current, "client", None)
    return ""


def _uses_deepseek(client: Any) -> bool:
    return "deepseek" in _llm_model_name(client).rsplit("/", 1)[-1].casefold()


def _is_json_schema_response_format_unsupported(error: Exception) -> bool:
    """Only downgrade the known structured-output capability rejection."""
    message = str(error).casefold()
    return "response_format" in message and (
        "unavailable" in message or "not support" in message or "unsupported" in message
    )


def _validate_response_schema(result: Any, schema: dict[str, Any]) -> None:
    """Keep local validation when the provider only guarantees a JSON object."""
    try:
        import jsonschema
    except ImportError:
        expected_type = schema.get("type")
        if expected_type == "object" and not isinstance(result, dict):
            raise ValueError("响应类型不符合Schema: 期望 object") from None
        if expected_type == "array" and not isinstance(result, list):
            raise ValueError("响应类型不符合Schema: 期望 array") from None
        return
    jsonschema.validate(instance=result, schema=schema)


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


# Event fields upstream marks required but validates as warning-only in
# ``_validate_output``.  Enforcing them as a strict structured-output schema
# makes providers reject otherwise-usable chunks and retry to the limit, so a
# single omitted field discards the whole chunk.  We relax them to match what
# upstream actually tolerates, then backfill defaults in ``_repair_extract_response``.
_SOFT_EVENT_FIELDS = ("references", "title", "content", "is_valid")


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
        for field in _SOFT_EVENT_FIELDS:
            _without_required_field(event, field)
        # Drop the ``minItems: 1`` floor on references: upstream only warns on
        # empty references, it never fails validation on them.
        references = event.get("properties", {}).get("references")
        if isinstance(references, dict):
            references.pop("minItems", None)
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
        # Backfill the warning-only fields so the item stays schema-shaped for
        # the downstream parser even when the model dropped them.
        if not isinstance(item.get("references"), list):
            item["references"] = []
            repaired.add("data.items[].references")
        if not isinstance(item.get("title"), str):
            item["title"] = ""
            repaired.add("data.items[].title")
        if not isinstance(item.get("content"), str):
            item["content"] = ""
            repaired.add("data.items[].content")
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
    telemetry-only ``data.meta`` or per-item fields (``is_valid``, ``references``,
    ``title``, ``content``).  Upstream zleap-sag lists these as schema-required,
    yet its own ``_validate_output`` only *warns* on missing/empty
    ``references``/``title``/``content`` and defaults ``is_valid`` to true — so a
    single omitted field makes structured-output providers reject and retry the
    whole chunk to the limit, discarding otherwise-usable events.  We relax the
    schema to what upstream actually tolerates and restore compatible defaults
    before zleap-sag's own output validator runs.
    """

    from zleap.sag.modules.extract.processor import EventProcessor

    current = EventProcessor._call_llm_with_retry
    if getattr(current, "_sag_api_extract_meta_compat", False):
        return

    async def _patched_call_llm_with_retry(self, messages, schema):  # type: ignore[no-untyped-def]
        active_schema = schema
        if _looks_like_extract_response_schema(schema):
            active_schema = _relax_extract_schema(schema)
        if _uses_deepseek(self.llm_client):
            log.info("DeepSeek 固定使用 response_format=json_object")
            result = await self.llm_client.chat_with_schema(
                messages,
                response_schema=None,
                response_format={"type": "json_object"},
            )
            _validate_response_schema(result, active_schema)
        else:
            try:
                result = await current(self, messages, active_schema)
            except Exception as error:
                if not _is_json_schema_response_format_unsupported(error):
                    raise
                log.warning("模型不支持 response_format=json_schema，降级为 json_object")
                result = await self.llm_client.chat_with_schema(
                    messages,
                    response_schema=None,
                    response_format={"type": "json_object"},
                )
                _validate_response_schema(result, active_schema)
        repaired = _repair_extract_response(result)
        if repaired:
            log.info("已兼容补齐 zleap-sag 事项抽取响应字段：%s", ", ".join(sorted(repaired)))
        return result

    _patched_call_llm_with_retry._sag_api_extract_meta_compat = True  # type: ignore[attr-defined]
    EventProcessor._call_llm_with_retry = _patched_call_llm_with_retry
