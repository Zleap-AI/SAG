"""Bounded lexical query analysis for retrieval."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

_QUERY_NOISE = (
    "知识库",
    "资料库",
    "资料中",
    "文档中",
    "告诉我",
    "帮我查",
    "搜索",
    "查询",
    "请问",
    "关于",
    "最新",
    "最近",
    "动态",
    "消息",
    "新闻",
    "内容",
    "资料",
    "一下",
    "是什么",
    "有哪些",
    "有什么",
)
_QUERY_INSTRUCTION_TERMS = frozenset(
    {
        "如何",
        "怎么",
        "怎样",
        "为什么",
        "为何",
        "是否",
        "能否",
        "进行",
        "介绍",
        "说明",
        "解释",
    }
)
_CHINESE_RUN_RE = re.compile(r"[\u3400-\u9fff]+")
_LEXICAL_PART_RE = re.compile(
    r"[a-z0-9][a-z0-9_.+-]{1,31}|[\u3400-\u9fff]+",
)
_LEGACY_TERM_RE = re.compile(
    r"[a-z0-9][a-z0-9_.+-]{1,31}|[\u3400-\u9fff]{2,16}",
)
# Letter and digit runs inside a single lexical token. A glued query such as
# "ai2027" or "gpt4" is one token to the regexes above, but the deterministic
# grep path matches literal substrings, so it can never bridge a query that
# glues letters to digits against content that separates them ("AI 2027",
# "GPT-4"). Splitting on the letter/digit boundary is the English/number
# analogue of the Chinese segmentation that lets contiguous queries match
# spaced evidence.
_ALNUM_SUBTOKEN_RE = re.compile(r"[a-z]+|[0-9]+")

Segmenter = Callable[[str], Iterable[str]]


@dataclass(frozen=True, slots=True)
class QueryAnalysis:
    normalized_phrase: str
    scoring_terms: tuple[str, ...]
    lookup_terms: tuple[str, ...]
    chinese_segmentation_used: bool
    expanded_terms: tuple[str, ...] = ()


def normalize_lexical_text(value: str) -> str:
    return "".join(re.findall(r"[a-z0-9\u3400-\u9fff]+", value.lower()))


def _remove_query_noise(query: str) -> str:
    cleaned = query.strip().lower()
    for phrase in _QUERY_NOISE:
        cleaned = cleaned.replace(phrase, " ")
    return cleaned


def _is_valid_term(value: str) -> bool:
    normalized = normalize_lexical_text(value)
    return len(normalized) >= 2 and not normalized.isdigit()


def _split_alnum_terms(
    parts: Iterable[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    terms: list[str] = []
    expanded_terms: list[str] = []
    keys: set[str] = set()
    for part in parts:
        subtokens = _ALNUM_SUBTOKEN_RE.findall(part)
        split = (
            part.isalnum()
            and len(subtokens) == 2
            and any(value.isalpha() for value in subtokens)
            and any(value.isdigit() for value in subtokens)
        )
        for candidate in subtokens if split else (part,):
            value = candidate.strip().lower()
            key = normalize_lexical_text(value)
            valid = len(key) >= 2 and (split or not key.isdigit())
            if not valid or key in keys:
                continue
            terms.append(value)
            if split:
                expanded_terms.append(value)
            keys.add(key)
            if len(terms) >= 4:
                return tuple(terms), tuple(expanded_terms)
    return tuple(terms), tuple(expanded_terms)


def _lookup_terms(
    scoring_terms: tuple[str, ...],
    phrase: str,
) -> tuple[str, ...]:
    phrase_term = (phrase,) if _is_valid_term(phrase) else ()
    candidates = (*scoring_terms, *phrase_term)
    terms: list[str] = []
    keys: set[str] = set()
    for value in candidates:
        key = normalize_lexical_text(value)
        if not key or key in keys:
            continue
        terms.append(value)
        keys.add(key)
        if len(terms) >= 4:
            break
    return tuple(terms)


def _legacy_query_terms(
    cleaned: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    return _split_alnum_terms(_LEGACY_TERM_RE.findall(cleaned))


def _chinese_runs(cleaned: str) -> tuple[str, ...]:
    return tuple(_CHINESE_RUN_RE.findall(cleaned))


def _jieba_segment(text: str) -> Iterable[str]:
    import jieba

    return jieba.cut(text, cut_all=False)


def _segmented_terms(
    cleaned: str,
    segmenter: Segmenter,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    values: list[str] = []
    for part in _LEXICAL_PART_RE.findall(cleaned):
        if _CHINESE_RUN_RE.fullmatch(part):
            if len(part) < 2:
                continue
            values.extend(segmenter(part))
        else:
            values.append(part)
    informative = (
        value
        for value in values
        if normalize_lexical_text(value) not in _QUERY_INSTRUCTION_TERMS
    )
    return _split_alnum_terms(informative)


def analyze_query(
    query: str,
    *,
    segmentation_enabled: bool = True,
    segmenter: Segmenter | None = None,
) -> QueryAnalysis:
    cleaned = _remove_query_noise(query)
    phrase = normalize_lexical_text(cleaned)
    legacy_terms, legacy_expanded_terms = _legacy_query_terms(cleaned)
    legacy_lookup_terms = _lookup_terms(legacy_terms, phrase)
    chinese_runs = _chinese_runs(cleaned)
    if not segmentation_enabled or not chinese_runs:
        return QueryAnalysis(
            phrase,
            legacy_terms,
            legacy_lookup_terms,
            False,
            legacy_expanded_terms,
        )

    try:
        scoring_terms, expanded_terms = _segmented_terms(
            cleaned,
            segmenter or _jieba_segment,
        )
    except Exception:  # noqa: BLE001 -- retrieval must survive tokenizer failure
        return QueryAnalysis(
            phrase,
            legacy_terms,
            legacy_lookup_terms,
            False,
            legacy_expanded_terms,
        )

    return QueryAnalysis(
        phrase,
        scoring_terms,
        _lookup_terms(scoring_terms, phrase),
        True,
        expanded_terms,
    )


def query_terms(query: str, *, segmentation_enabled: bool = True) -> list[str]:
    """Return bounded lookup terms for compatibility with existing callers."""

    return list(analyze_query(query, segmentation_enabled=segmentation_enabled).lookup_terms)
