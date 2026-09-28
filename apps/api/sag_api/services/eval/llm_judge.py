"""LLM-as-judge:比较两组检索结果对回答给定问题谁更有帮助。

结论:pairwise winner + 简短理由。复用现有 `LLMClient.complete`,不引第二个模型配置。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from sag_api.core.logging import get_logger
from sag_api.sag import RetrievedSection

log = get_logger("eval.judge")

_EXCERPT_LIMIT = 400  # 每条 section 塞给 judge 的最大字符,避免 prompt 撑爆
_MAX_SECTIONS_PER_SIDE = 8
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass(frozen=True, slots=True)
class JudgeVerdict:
    """Pairwise 结果。winner 归一化到 'A' | 'B' | 'tie'。"""

    winner: str
    reason: str
    raw: str
    a_label: str
    b_label: str

    def to_dict(self) -> dict[str, str]:
        return {
            "winner": self.winner,
            "reason": self.reason,
            "raw": self.raw,
            "a_label": self.a_label,
            "b_label": self.b_label,
        }


def _clip(text: str, limit: int = _EXCERPT_LIMIT) -> str:
    text = (text or "").strip().replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _format_sections(sections: list[RetrievedSection]) -> str:
    trimmed = sections[:_MAX_SECTIONS_PER_SIDE]
    if not trimmed:
        return "(空)"
    lines: list[str] = []
    for index, section in enumerate(trimmed, 1):
        heading = _clip(section.heading, 120) or "(无标题)"
        content = _clip(section.content)
        score = f"{float(section.score or 0.0):.3f}"
        lines.append(f"[{index}] score={score} 标题:{heading}\n    片段:{content}")
    return "\n".join(lines)


def build_pairwise_prompt(
    query: str,
    a_label: str,
    a_sections: list[RetrievedSection],
    b_label: str,
    b_sections: list[RetrievedSection],
) -> list[dict[str, str]]:
    """构造 pairwise judge 用的 chat messages。"""

    system = (
        "你是检索结果评审。给定一个用户问题和两组检索证据,判断哪一组更能支撑对问题的准确回答。"
        "只看证据本身;不要引入外部知识。"
        "只用 JSON 输出:{\"winner\": \"A\" | \"B\" | \"tie\", \"reason\": \"一句话说明\"}。"
        "偏向覆盖度更高、更贴题、噪声更少的一组;两组均无法作答时选 tie。"
    )
    user = (
        f"问题:{query.strip()}\n\n"
        f"=== 结果 A(策略:{a_label}) ===\n{_format_sections(a_sections)}\n\n"
        f"=== 结果 B(策略:{b_label}) ===\n{_format_sections(b_sections)}\n"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _parse_verdict(text: str) -> tuple[str, str]:
    """从 LLM 原文里抽 winner/reason;失败一律回退到 tie 而不是抛。"""
    match = _JSON_RE.search(text or "")
    if not match:
        return "tie", "judge 未返回可解析的 JSON"
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return "tie", "judge 返回的 JSON 无法解析"
    raw_winner = str(data.get("winner", "")).strip().upper()
    winner = raw_winner if raw_winner in {"A", "B", "TIE"} else "TIE"
    reason = str(data.get("reason", "") or "").strip() or "judge 未提供理由"
    return winner.lower() if winner == "TIE" else winner, reason


async def judge_pairwise(
    llm: Any,
    query: str,
    a_label: str,
    a_sections: list[RetrievedSection],
    b_label: str,
    b_sections: list[RetrievedSection],
) -> JudgeVerdict | None:
    """跑一次 pairwise judge;LLM 未配置或调用失败返回 None。

    None 语义:调用方应把结果视为「未打分」,不要伪装成 tie 混入胜率统计。
    """

    if llm is None or not getattr(llm, "configured", False):
        return None
    messages = build_pairwise_prompt(query, a_label, a_sections, b_label, b_sections)
    try:
        raw = await llm.complete(messages)
    except Exception as error:  # noqa: BLE001
        log.warning("LLM judge 调用失败:%s", error)
        return None
    winner, reason = _parse_verdict(raw)
    return JudgeVerdict(
        winner=winner,
        reason=reason,
        raw=raw,
        a_label=a_label,
        b_label=b_label,
    )
