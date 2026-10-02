"""Query Responses thinking policy; no extraction provider registration."""

def normalize_thinking(request, thinking_rules):
    """Adapt guarded stock defaults, before LiteLLM validates the custom route."""
    model = str(request.get("model", ""))
    if not model.startswith("sag_chatbot_responses/"):
        return request
    from .errors import provider_error

    model_id = model.rsplit("/", 1)[-1].casefold()
    rule = thinking_rules.get(model_id, {})
    qwen = "qwen" in model.casefold()
    normalized = dict(request)
    extra = dict(normalized.get("extra_body") or {})
    effort = normalized.pop("reasoning_effort", None)
    if rule.get("legacy_disabled_field") == "thinking" and extra.get("thinking") == {"type": "disabled"}:
        del extra["thinking"]
        if effort is None:
            effort = "none"
    if (qwen or rule.get("legacy_disabled_field") == "enable_thinking") and extra.get("enable_thinking") is False:
        del extra["enable_thinking"]
        if effort is None:
            effort = "none"
    if effort in (None, "none"):
        effort = rule.get("default_effort", effort)
    # Preserve native settings, including summary fields, across repeated policy
    # application (chat construction followed by the lifespan-installed hook).
    reasoning = extra.get("reasoning", {})
    if effort is not None and isinstance(reasoning, dict) and "effort" not in reasoning:
        if effort == "none" and qwen and not rule:
            raise provider_error(
                422, "Responses cannot infer thinking-off support for this Qwen model; "
                "set SAG_CHATBOT_LLM_EXTRA_BODY reasoning.effort or add a model thinking rule supported by your endpoint"
            )
        extra["reasoning"] = {**reasoning, "effort": effort}
    if extra or "extra_body" in normalized:
        normalized["extra_body"] = extra
    return normalized
