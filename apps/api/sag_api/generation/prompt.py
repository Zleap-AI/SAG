"""答案生成的提示词与引用构造。"""

from __future__ import annotations

from typing import Any

from sag_api.branding import DEFAULT_AGENT_NAME
from sag_api.sag import RetrievedSection


def _citation_excerpt(content: str) -> str:
    """Return a bounded source excerpt without assigning it event semantics."""
    text = " ".join(content.split())
    if not text:
        return ""
    excerpt_limit = 720
    excerpt = text[:excerpt_limit].strip()
    if len(text) > excerpt_limit:
        excerpt = excerpt.rstrip("…") + "…"
    return excerpt


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def _event_refs_by_section(events: list[Any] | None) -> dict[tuple[str, str], list[dict[str, str]]]:
    """Index traceable extracted events by source config and chunk.

    Event order comes from ``graph_for_sections`` and is preserved.  The
    composite key is required because chunk identifiers are only source-local.
    """

    grouped: dict[tuple[str, str], list[dict[str, str]]] = {}
    seen: dict[tuple[str, str], set[str]] = {}
    for event in events or []:
        source_config_id = str(_field(event, "source_config_id") or "").strip()
        chunk_id = str(_field(event, "chunk_id") or "").strip()
        event_id = str(_field(event, "id") or "").strip()
        title = " ".join(str(_field(event, "title") or "").split())
        if not source_config_id or not chunk_id or not event_id or not title:
            continue
        key = (source_config_id, chunk_id)
        if event_id in seen.setdefault(key, set()):
            continue
        seen[key].add(event_id)
        ref = {
            "id": event_id,
            "title": title[:500],
            "summary": " ".join(str(_field(event, "summary") or "").split())[:800],
            "category": " ".join(str(_field(event, "category") or "").split())[:100],
        }
        content = " ".join(str(_field(event, "content") or "").split())[:4000]
        if content:
            ref["content"] = content
        grouped.setdefault(key, []).append(ref)
    return grouped


_GUIDANCE = {
    "zh": (
        "【工作目标】\n"
        "- 以解决用户问题并交付可直接使用的结果为首要目标。先在内部明确目标、对象、范围、时限、"
        "约束和成功标准，再决定是直接回答、澄清还是调用工具；不要输出冗长的内部思维过程。\n"
        "【澄清与推进】\n"
        "- 若缺少的信息会实质改变结论或交付物，先集中提出 1–3 个简短、可回答的关键问题，"
        "不要带着根本歧义盲目检索。次要信息可采用合理默认值，明确说明假设后继续推进，避免过度反问。\n"
        "- 复杂任务先拆成必要步骤并持续推进；每次工具结果都用于更新判断。证据不足、过旧或冲突时，"
        "应调整查询、时间范围或工具，而不是重复同一搜索。达到交付标准后及时收敛。\n"
        "【时间与事实】\n"
        "- 把时间视为事实的一部分。问题涉及最近、最新、当前、相对日期、版本、价格、政策、日程等"
        "时效信息时，先调用 get_time 建立准确的当前时间锚点，再把绝对日期、合适的时间窗口和查询对象"
        "写入后续检索；不得沿用旧对话、模型记忆或用户示例中的年份充当当前时间。\n"
        "- 区分发布时间、事件发生时间和生效时间，优先采用与目标时间范围匹配且可核验的资料。"
        "当事实核查、专业分析、比较、推荐或数据问题的结论依赖外部事实或时效信息时，"
        "应积极使用最匹配的知识库或挂载工具，不要把未经核查的模型记忆写成事实。\n"
        "【证据策略】\n"
        "- 外部检索优先采用官方公告、产品文档、原始数据、标准/监管文件、论文等一手资料；"
        "新闻、行业分析等二手资料用于补充背景，搜索摘要、聚合页和转载不能单独支撑关键结论。\n"
        "- 对重要或时效性强的结论，在条件允许时用至少两个相互独立的来源交叉核验；"
        "发生冲突时比较资料的直接性、权威性、发布时间和目标时间范围。只有低质量、冲突或无法打开的"
        "证据时继续检索，仍无法确认就明确标为未核实，不用看似合理的模型记忆补齐。\n"
        "【工具使用】\n"
        "- 工具只在任务确实需要时使用。寒暄、致谢、告别、身份询问应直接回答，不调用检索。"
        "纯创作、简单计算，以及仅处理用户已提供内容的翻译、改写或总结，也不要为了补充信息而检索；"
        "请求存在会改变结果的歧义时先澄清，不能用搜索代替澄清。\n"
        "- 涉及本地知识库、上传文档或 @ 范围时使用 search_context；必要时从不同角度改写查询，"
        "并用 get_entity 做实体消歧和形成后续检索词；关键事实仍须由 search_context 或带可追溯来源的工具"
        "确认。没有合适工具或工具失败时，说明无法核查的部分和已知边界。\n"
        "【回答规范】\n"
        "- 结论和可执行结果优先，综合证据并区分事实、推断、假设和资料缺口；简洁、分层，"
        "不照抄工具输出。需要用户决策时给出清晰选项、取舍和下一步。\n"
        "- 只有 search_context 返回的编号才能写成 [n]，并放在对应论断后；绝不虚构编号。"
        "其他工具返回的 URL 保留为 Markdown 链接，不自行编号。只要使用了带 URL 的外部检索或阅读工具，"
        "关键外部事实就必须在对应论断附近附上可点击的直接来源；无法形成可追溯来源时，明确证据缺口，"
        "不要把该事实写成已确认。\n"
        "- 输出前检查：是否回答了真实目标，关键事实是否足够新且有来源，日期与数字是否准确，"
        "引用是否能打开，是否明确了仍存在的不确定性。"
    ),
    "en": (
        "[Delivery objective]\n"
        "- Optimize for solving the user's real problem and delivering a directly usable result. Internally "
        "establish the goal, audience, scope, time horizon, constraints, and success criteria before deciding "
        "whether to answer, clarify, or use tools. Do not expose lengthy hidden reasoning.\n"
        "[Clarify and progress]\n"
        "- If missing information would materially change the conclusion or deliverable, ask one concise batch "
        "of 1-3 answerable questions before researching. For minor gaps, state reasonable assumptions and proceed.\n"
        "- Break complex work into necessary steps and update the approach from each tool result. If evidence is "
        "thin, stale, or conflicting, change the query, time window, or tool instead of repeating the same search.\n"
        "[Time and facts]\n"
        "- Treat time as part of every time-sensitive fact. For latest, recent, current, relative-date, version, "
        "price, policy, or schedule requests, call get_time first, then put absolute dates, an appropriate time "
        "window, and the subject into subsequent searches. Never treat an old conversation date, model memory, "
        "or a year in the user's example as the current date.\n"
        "- Distinguish publication, event, and effective dates. When factual research, analysis, comparisons, "
        "recommendations, or data depend on external or time-sensitive facts, use the best available knowledge "
        "or mounted tool instead of presenting unverified memory as fact.\n"
        "[Evidence strategy]\n"
        "- For external research, prefer first-party announcements, product documentation, original data, "
        "standards or regulator material, and research papers. Use reputable secondary reporting for context; "
        "search snippets, aggregators, and reposts cannot alone support a key claim.\n"
        "- When feasible, cross-check important or fast-changing claims with at least two independent sources. "
        "Resolve conflicts by directness, authority, publication date, and fit to the target time window. If only "
        "weak, conflicting, or inaccessible evidence remains, keep researching or mark the claim unverified; do "
        "not fill the gap with plausible model memory.\n"
        "[Tool use]\n"
        "- Use tools only when the task actually requires them. Answer greetings, thanks, farewells, and "
        "identity questions directly without retrieval. Do not retrieve for pure creation, simple arithmetic, "
        "or translation, rewriting, and summarization based only on content the user supplied. Clarify material "
        "ambiguity first; search is not a substitute for clarification.\n"
        "- Use search_context for local knowledge, uploads, or an @ scope; reformulate from another angle and use "
        "get_entity only to disambiguate entities and shape later searches. Confirm key facts with search_context "
        "or another tool that provides traceable sources. State verification limits when no suitable tool works.\n"
        "[Answer rules]\n"
        "- Lead with the conclusion and usable output. Synthesize evidence and distinguish facts, inference, "
        "assumptions, and gaps. When a decision is needed, give options, tradeoffs, and a next step.\n"
        "- Only search_context numbers may be cited as [n], near the supported claim. Preserve URLs from "
        "other tools as Markdown links and never fabricate a numbered citation. Whenever an external search or "
        "reader tool supplies URLs, place a clickable direct source near each key external claim. If no traceable "
        "source can be formed, state the evidence gap instead of presenting the claim as confirmed.\n"
        "- Before finishing, verify that the real goal was answered, evidence is fresh enough, dates and numbers "
        "are accurate, citations open, and remaining uncertainty is explicit."
    ),
}

_TIME_RULE = {
    "zh": (
        "当前场景：系统时区为「{timezone}」。数据库和 API 时间戳统一为 UTC；"
        "面向用户解释和展示时按系统时区转换。当前日期和时间是动态事实，"
        "必须在相关任务中调用 get_time 获取，不得根据提示词、历史消息或模型知识猜测。"
    ),
    "en": (
        "Current context: the configured system timezone is {timezone}. Database and API timestamps use UTC; "
        "convert them for the user. The current date and time are dynamic facts: call get_time for relevant "
        "tasks and never infer them from the prompt, conversation history, or model knowledge."
    ),
}

_IDENTITY = {
    "zh": "你的名字是「{name}」。当用户询问你的身份或名字时，使用这个名称回答。",
    "en": "Your name is {name}. Use this name when the user asks who you are.",
}

_USER_TEMPLATE = {
    "zh": "资料：\n{context}\n\n问题：{query}\n\n请依据资料作答，并在相应位置用 [序号] 标注引用。",
    "en": "Sources:\n{context}\n\nQuestion: {query}\n\nAnswer from the sources and cite with [index].",
}


def estimate_tokens(text: str) -> int:
    """CJK 感知的 token 估算：中日韩 ≈1/字，其余 ≈1/4 字符（与前端口径一致）。"""
    cjk = sum(1 for ch in text if "\u3000" <= ch <= "\u9fff" or "\uf900" <= ch <= "\ufaff")
    return cjk + max(0, (len(text) - cjk) + 3) // 4


def _format_context(sections: list[RetrievedSection]) -> str:
    if not sections:
        return "（无相关资料）"
    blocks = []
    for i, s in enumerate(sections, start=1):
        heading = s.heading or "片段"
        blocks.append(f"[{i}] {heading}\n{s.content}")
    return "\n\n".join(blocks)


def _identity_prompt(name: str, language: str) -> str:
    display_name = name.strip() or DEFAULT_AGENT_NAME
    return _IDENTITY[language].format(name=display_name)


def build_messages(
    query: str,
    sections: list[RetrievedSection],
    *,
    history: list[dict[str, str]] | None = None,
    language: str = "zh",
    name: str = DEFAULT_AGENT_NAME,
) -> list[dict[str, str]]:
    lang = language if language in _GUIDANCE else "zh"
    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": "\n\n".join((_identity_prompt(name, lang), _GUIDANCE[lang])),
        }
    ]
    if history:
        messages.extend(history)
    messages.append(
        {
            "role": "user",
            "content": _USER_TEMPLATE[lang].format(context=_format_context(sections), query=query),
        }
    )
    return messages


def build_agent_messages(
    name: str,
    persona: dict[str, Any],
    query: str,
    *,
    history: list[dict[str, str]] | None = None,
    language: str = "zh",
    timezone: str = "Asia/Shanghai",
    attachments: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """注入 Agent 设定（agent-first：无预置资料区，检索由工具按需完成）。"""
    lang = language if language in _GUIDANCE else "zh"
    persona = persona or {}
    parts = [_identity_prompt(name, lang)]
    system_prompt = str(persona.get("system_prompt") or "").strip()
    if system_prompt:
        parts.append(system_prompt)
    parts.append(_GUIDANCE[lang])
    parts.append(_TIME_RULE[lang].format(timezone=timezone))
    guardrails = persona.get("guardrails") or []
    if guardrails:
        parts.append("约束：" + "；".join(guardrails))
    empty_response = (persona.get("empty_response") or "").strip()
    if empty_response:
        parts.append(f"若检索后仍无相关资料，用这句话回应：「{empty_response}」")
    messages: list[dict[str, str]] = [{"role": "system", "content": "\n\n".join(parts)}]
    if history:
        messages.extend(history)
    user_text = query
    if attachments:
        # 视觉输入：OpenAI 兼容 content parts（图片读盘转 data URL；历史轮仅保留文本）
        import base64

        content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
        for att in attachments:
            path, media_type = att.get("path"), att.get("media_type", "image/png")
            if not path:
                continue
            try:
                with open(path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode()
            except OSError:
                continue
            content.append({"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64}"}})
        messages.append({"role": "user", "content": content})
    else:
        messages.append({"role": "user", "content": user_text})
    return messages


def build_prompt_preview(messages: list[dict[str, Any]], *, limit: int = 6000) -> str:
    """把运行开始前的输入上下文拼成可读预览。

    多模态消息（content 为 parts 列表）：文本部分原样、图片以占位符呈现（不吐 base64）。
    """
    lines: list[str] = []
    current_user_index = next(
        (index for index in range(len(messages) - 1, -1, -1) if messages[index].get("role") == "user"),
        -1,
    )
    history_labels = {"user": "历史 · 用户", "assistant": "历史 · 助手", "tool": "历史 · 工具"}
    for index, m in enumerate(messages):
        role = m.get("role", "")
        if role == "system":
            label = "系统指令"
        elif role == "user" and index == current_user_index:
            label = "当前问题"
        else:
            label = history_labels.get(role, role)
        content = m.get("content", "")
        if isinstance(content, list):
            texts = [p.get("text", "") for p in content if p.get("type") == "text"]
            images = sum(1 for p in content if p.get("type") == "image_url")
            content = "\n".join(texts) + (f"\n〔附图 ×{images}〕" if images else "")
        lines.append(f"【{label}】\n{content}")
    text = "\n\n".join(lines)
    if len(text) > limit:
        # 保留系统指令的开头与当前问题所在的尾部；仅从中间压缩历史，避免
        # 透明面板反而把本轮真正输入截掉。
        head = max(1, int(limit * 0.62))
        tail = max(1, limit - head)
        text = text[:head].rstrip() + "\n\n…（中间历史上下文已截断）…\n\n" + text[-tail:].lstrip()
    return text


def build_citations(
    sections: list[RetrievedSection],
    source_refs: dict[str, dict[str, str]] | None = None,
    events: list[Any] | None = None,
) -> list[dict[str, Any]]:
    """由检索段落确定性地构造引用列表（编号与 prompt 中一致）。

    `source_refs`：{sag_source_config_id: {"id": sag 信源 id, "name": 信源名}}。
    `events`：`graph_for_sections` 返回的真实抽取事件；按
    `(source_config_id, chunk_id)` 关联，每条引用最多附带三个事件。
    对外的 `source_id` 一律指 **sag 信源 id**（可直接路由 / 取原文），不泄漏引擎内部 id。
    `event_refs[].content` 是抽取后的事项正文；`snippet` 仅用于原文定位，
    不从分块正文推断或伪造事项正文。
    """
    citations = []
    event_refs = _event_refs_by_section(events)
    for i, s in enumerate(sections, start=1):
        snippet = _citation_excerpt(s.content)
        ref = (source_refs or {}).get(s.source_config_id or "") or {}
        citation = {
            "kind": "internal",
            "n": i,
            "chunk_id": s.chunk_id,
            "heading": s.heading,
            "snippet": snippet,
            "score": round(s.score, 4),
            "source_id": ref.get("id"),
            "source_name": ref.get("name"),
        }
        event_key = ((s.source_config_id or "").strip(), (s.chunk_id or "").strip())
        matched_events = event_refs.get(event_key, [])[:3]
        if matched_events:
            citation["event_refs"] = matched_events
        citations.append(citation)
    return citations
