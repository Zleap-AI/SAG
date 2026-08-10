"""离线检索评测:同一 golden 集在多个策略下跑,产出 recall/nDCG/latency 对照。

用法示例:
    uv run python scripts/eval_retrieval.py \\
        --fixture tests/fixtures/golden_queries.yaml \\
        --strategies vector,multi_es_fast \\
        --mode both \\
        --judge on \\
        --out /tmp/eval_report.json

`--mode`:
    raw      仅跑引擎裸召回(engine_manager.search_many),不走 rerank/词法融合。
    full     跑完整 retrieve_relevant_sections 流水线(rerank + 词法 + 隐藏过滤)。
    both     两个都跑,产出两份对照。汇报里推荐 both。

引擎和数据来自当前 sag_api 的实际配置(DATABASE_URL、vector provider 等)。
本脚本从 SessionLocal 直接读 Source,不启 HTTP 服务。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 允许从 apps/api/scripts 目录直接执行:把仓库根塞进 sys.path。
_HERE = Path(__file__).resolve()
_APP_ROOT = _HERE.parent.parent  # apps/api
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))

import yaml  # noqa: E402  # 在 sys.path 调整后导入,IDE 提示可忽略

from sag_api.core.config import settings  # noqa: E402
from sag_api.core.db import SessionLocal  # noqa: E402
from sag_api.generation import LLMClient  # noqa: E402
from sag_api.sag import EngineManager, SearchOutcome  # noqa: E402
from sag_api.services.eval.llm_judge import judge_pairwise  # noqa: E402
from sag_api.services.retrieval_service import retrieve_relevant_sections  # noqa: E402
from sag_api.services.source_service import search_source_candidates  # noqa: E402


@dataclass(slots=True)
class GoldenCase:
    id: str
    query: str
    source_ids: list[str] | None
    expected: set[str]  # rel=2
    related: set[str]  # rel=1
    tags: list[str] = field(default_factory=list)
    notes: str = ""


def _load_fixture(path: Path) -> list[GoldenCase]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    cases: list[GoldenCase] = []
    for index, item in enumerate(raw):
        cases.append(
            GoldenCase(
                id=str(item.get("id") or f"q{index:03d}"),
                query=str(item.get("query", "")).strip(),
                source_ids=list((item.get("scope") or {}).get("source_ids") or []) or None,
                expected={str(cid) for cid in (item.get("expected_chunk_ids") or [])},
                related={str(cid) for cid in (item.get("related_chunk_ids") or [])},
                tags=list(item.get("tags") or []),
                notes=str(item.get("notes") or ""),
            )
        )
    return [case for case in cases if case.query]


def _recall_at_k(gold: set[str], hits: list[str], k: int) -> float | None:
    if not gold:
        return None
    top = hits[:k]
    matched = sum(1 for chunk in top if chunk in gold)
    return matched / len(gold)


def _mrr(gold: set[str], hits: list[str], cap: int = 10) -> float | None:
    if not gold:
        return None
    for index, chunk in enumerate(hits[:cap], 1):
        if chunk in gold:
            return 1.0 / index
    return 0.0


def _ndcg(expected: set[str], related: set[str], hits: list[str], k: int = 10) -> float | None:
    if not expected and not related:
        return None
    # rel=2 for expected,rel=1 for related;grade 0 for the rest。
    grades = [(2 if chunk in expected else 1 if chunk in related else 0) for chunk in hits[:k]]
    dcg = sum((2 ** grade - 1) / math.log2(index + 2) for index, grade in enumerate(grades))
    ideal_grades = sorted(
        [2] * len(expected) + [1] * len(related),
        reverse=True,
    )[:k]
    idcg = sum((2 ** grade - 1) / math.log2(index + 2) for index, grade in enumerate(ideal_grades))
    return (dcg / idcg) if idcg > 0 else 0.0


@dataclass(slots=True)
class OneRunMetrics:
    case_id: str
    strategy: str
    mode: str  # raw | full
    latency_ms: float
    hit_chunk_ids: list[str]
    recall_at_5: float | None
    recall_at_10: float | None
    mrr_at_10: float | None
    ndcg_at_10: float | None
    engine_stats: dict[str, Any]
    error: str | None = None


async def _run_raw(
    engine_manager: EngineManager,
    sources: list[Any],
    query: str,
    strategy: str,
) -> tuple[SearchOutcome, float]:
    """裸引擎召回:走 search_many,不做 rerank/词法融合。"""
    targets = [(src.sag_source_config_id, src) for src in sources]
    start = time.perf_counter()
    outcome = await engine_manager.search_many(
        targets,
        query,
        strategy=strategy,
        top_k=settings.search_top_k,
    )
    return outcome, round((time.perf_counter() - start) * 1000, 2)


async def _run_full(
    engine_manager: EngineManager,
    sources: list[Any],
    query: str,
    strategy: str,
) -> tuple[SearchOutcome, float]:
    """完整链路:含 rerank + 词法 + 隐藏过滤。latency 从 stats.latency_total_ms 读。"""
    start = time.perf_counter()
    outcome = await retrieve_relevant_sections(
        engine_manager,
        sources,
        query,
        strategy=strategy,
        top_k=settings.search_top_k,
    )
    latency_ms = float(
        outcome.stats.get("latency_total_ms")
        or round((time.perf_counter() - start) * 1000, 2),
    )
    return outcome, latency_ms


async def _one_case_run(
    engine_manager: EngineManager,
    case: GoldenCase,
    strategy: str,
    mode: str,
    all_sources: list[Any],
    sources_by_id: dict[str, Any],
) -> OneRunMetrics:
    if case.source_ids:
        sources = [sources_by_id[sid] for sid in case.source_ids if sid in sources_by_id]
        if not sources:
            return OneRunMetrics(
                case_id=case.id,
                strategy=strategy,
                mode=mode,
                latency_ms=0.0,
                hit_chunk_ids=[],
                recall_at_5=None,
                recall_at_10=None,
                mrr_at_10=None,
                ndcg_at_10=None,
                engine_stats={},
                error=f"scope 里的 source_id 都不在库里({case.source_ids})",
            )
    else:
        sources = all_sources

    try:
        if mode == "raw":
            outcome, latency = await _run_raw(engine_manager, sources, case.query, strategy)
        else:
            outcome, latency = await _run_full(engine_manager, sources, case.query, strategy)
    except Exception as error:  # noqa: BLE001
        return OneRunMetrics(
            case_id=case.id,
            strategy=strategy,
            mode=mode,
            latency_ms=0.0,
            hit_chunk_ids=[],
            recall_at_5=None,
            recall_at_10=None,
            mrr_at_10=None,
            ndcg_at_10=None,
            engine_stats={},
            error=getattr(error, "message", None) or str(error),
        )

    hits = [str(section.chunk_id) for section in outcome.sections if section.chunk_id]
    return OneRunMetrics(
        case_id=case.id,
        strategy=strategy,
        mode=mode,
        latency_ms=latency,
        hit_chunk_ids=hits,
        recall_at_5=_recall_at_k(case.expected, hits, 5),
        recall_at_10=_recall_at_k(case.expected, hits, 10),
        mrr_at_10=_mrr(case.expected, hits),
        ndcg_at_10=_ndcg(case.expected, case.related, hits),
        engine_stats=dict(outcome.stats),
    )


def _aggregate(metrics: list[OneRunMetrics]) -> dict[str, Any]:
    """按 strategy+mode 聚合到平均指标,便于汇报直接引用。"""
    grouped: dict[tuple[str, str], list[OneRunMetrics]] = {}
    for metric in metrics:
        grouped.setdefault((metric.strategy, metric.mode), []).append(metric)

    def _mean(values: list[float]) -> float | None:
        return round(statistics.fmean(values), 4) if values else None

    def _pct(values: list[float], q: float) -> float | None:
        if not values:
            return None
        try:
            return round(statistics.quantiles(values, n=100)[int(q * 100) - 1], 2)
        except statistics.StatisticsError:
            return round(max(values), 2)

    summary: list[dict[str, Any]] = []
    for (strategy, mode), items in sorted(grouped.items()):
        recalls5 = [m.recall_at_5 for m in items if m.recall_at_5 is not None]
        recalls10 = [m.recall_at_10 for m in items if m.recall_at_10 is not None]
        mrrs = [m.mrr_at_10 for m in items if m.mrr_at_10 is not None]
        ndcgs = [m.ndcg_at_10 for m in items if m.ndcg_at_10 is not None]
        latencies = [m.latency_ms for m in items if not m.error]
        summary.append(
            {
                "strategy": strategy,
                "mode": mode,
                "cases": len(items),
                "errors": sum(1 for m in items if m.error),
                "graded_cases": len(recalls10),
                "recall@5": _mean(recalls5),
                "recall@10": _mean(recalls10),
                "mrr@10": _mean(mrrs),
                "ndcg@10": _mean(ndcgs),
                "latency_ms_p50": _pct(latencies, 0.5) if latencies else None,
                "latency_ms_p95": _pct(latencies, 0.95) if latencies else None,
                "latency_ms_mean": _mean(latencies),
            }
        )
    return {"per_strategy": summary}


async def _run_judge(
    llm: LLMClient,
    cases: list[GoldenCase],
    metrics: list[OneRunMetrics],
    strategies: list[str],
    mode: str,
) -> list[dict[str, Any]]:
    """在每条 case 里 pairwise 打分。多于两策略时取相邻两两配对。"""
    if len(strategies) < 2 or not llm.configured:
        return []

    by_case: dict[str, dict[str, OneRunMetrics]] = {}
    for metric in metrics:
        if metric.mode != mode:
            continue
        by_case.setdefault(metric.case_id, {})[metric.strategy] = metric

    async def _one(case: GoldenCase, a: str, b: str) -> dict[str, Any] | None:
        record = by_case.get(case.id, {})
        a_metric = record.get(a)
        b_metric = record.get(b)
        if not a_metric or not b_metric or a_metric.error or b_metric.error:
            return None
        # 只把 top-k section 信息通过 hit_chunk_ids 传给 judge 不够——它没有正文。
        # 因此这里必须重新跑一次拿 sections。避免这次重跑的最简办法:evaluate 期间就把
        # sections 存到 metric 里。为控制内存,我们在 evaluate 时直接把 sections 存 metric.raw。
        # 若未存(mode=raw 场景),就跳过。
        pass  # 见下方 evaluate 主循环的说明。

    # 我们没有在 metric 上冗余持有 sections(避免报告体过大),judge 阶段单独再跑一次
    # 以拿到真实 sections。因为 judge 是可选的、样本量小,这个开销可接受。
    return await _judge_via_replay(llm, cases, strategies, mode)


async def _judge_via_replay(
    llm: LLMClient,
    cases: list[GoldenCase],
    strategies: list[str],
    mode: str,
) -> list[dict[str, Any]]:
    """re-run 一次拿 sections 后 pairwise 判分。仅在 --judge on 时调用。"""
    # 惰性 import 以避免上面主循环再度依赖。
    from sag_api.core.db import SessionLocal as _SL

    async with _SL() as session:
        all_sources = await search_source_candidates(session, None)
        await session.commit()
    sources_by_id = {source.id: source for source in all_sources}

    engine_manager = EngineManager(settings)
    verdicts: list[dict[str, Any]] = []
    try:
        for case in cases:
            sources = (
                [sources_by_id[sid] for sid in (case.source_ids or []) if sid in sources_by_id]
                or all_sources
            )
            if not sources:
                continue
            sections_by_strategy: dict[str, list[Any]] = {}
            for strategy in strategies:
                try:
                    if mode == "raw":
                        outcome, _ = await _run_raw(engine_manager, sources, case.query, strategy)
                    else:
                        outcome, _ = await _run_full(engine_manager, sources, case.query, strategy)
                    sections_by_strategy[strategy] = list(outcome.sections)
                except Exception:  # noqa: BLE001
                    sections_by_strategy[strategy] = []

            for a_index, a in enumerate(strategies):
                for b in strategies[a_index + 1 :]:
                    verdict = await judge_pairwise(
                        llm,
                        case.query,
                        a,
                        sections_by_strategy.get(a, []),
                        b,
                        sections_by_strategy.get(b, []),
                    )
                    if verdict is None:
                        continue
                    verdicts.append(
                        {
                            "case_id": case.id,
                            "mode": mode,
                            "a": a,
                            "b": b,
                            "winner": verdict.winner,
                            "reason": verdict.reason,
                        }
                    )
    finally:
        await engine_manager.aclose_all()
    return verdicts


def _judge_summary(verdicts: list[dict[str, Any]]) -> dict[str, Any]:
    """汇总 pairwise 胜率,含简单二项检验(近似)。"""
    if not verdicts:
        return {"pairs": []}
    buckets: dict[tuple[str, str, str], dict[str, int]] = {}
    for verdict in verdicts:
        key = (verdict["mode"], verdict["a"], verdict["b"])
        bucket = buckets.setdefault(key, {"A": 0, "B": 0, "tie": 0, "total": 0})
        outcome = verdict["winner"]
        outcome_key = outcome.upper() if outcome in {"A", "B"} else "tie"
        bucket[outcome_key] = bucket.get(outcome_key, 0) + 1
        bucket["total"] += 1

    def _wilson(k: int, n: int) -> tuple[float, float] | None:
        if n == 0:
            return None
        z = 1.96
        p = k / n
        denom = 1 + z * z / n
        centre = p + z * z / (2 * n)
        half = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
        return (max(0.0, (centre - half) / denom), min(1.0, (centre + half) / denom))

    pairs: list[dict[str, Any]] = []
    for (mode, a, b), bucket in sorted(buckets.items()):
        total = bucket["total"]
        wins_a = bucket.get("A", 0)
        wins_b = bucket.get("B", 0)
        ties = bucket.get("tie", 0)
        ci_a = _wilson(wins_a, total)
        pairs.append(
            {
                "mode": mode,
                "a": a,
                "b": b,
                "total": total,
                "wins_a": wins_a,
                "wins_b": wins_b,
                "ties": ties,
                "win_rate_a": round(wins_a / total, 4) if total else None,
                "win_rate_a_ci95": (
                    [round(ci_a[0], 4), round(ci_a[1], 4)] if ci_a else None
                ),
            }
        )
    return {"pairs": pairs}


async def _amain(args: argparse.Namespace) -> int:
    fixture_path = Path(args.fixture).resolve()
    if not fixture_path.exists():
        print(f"fixture 不存在: {fixture_path}", file=sys.stderr)
        return 2

    cases = _load_fixture(fixture_path)
    if not cases:
        print("fixture 里没有有效的 case,退出。", file=sys.stderr)
        return 2

    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    if len(strategies) < 1:
        print("--strategies 至少要有一个策略", file=sys.stderr)
        return 2

    modes = ["raw", "full"] if args.mode == "both" else [args.mode]

    async with SessionLocal() as session:
        all_sources = await search_source_candidates(session, None)
        await session.commit()
    sources_by_id = {source.id: source for source in all_sources}

    engine_manager = EngineManager(settings)
    metrics: list[OneRunMetrics] = []
    try:
        for case in cases:
            for strategy in strategies:
                for mode in modes:
                    metric = await _one_case_run(
                        engine_manager,
                        case,
                        strategy,
                        mode,
                        all_sources,
                        sources_by_id,
                    )
                    metrics.append(metric)
                    tag = "ok" if not metric.error else f"err({metric.error[:40]})"
                    print(
                        f"[{case.id}] strategy={strategy:<16} mode={mode:<5} "
                        f"latency={metric.latency_ms:>7.1f}ms "
                        f"recall@5={metric.recall_at_5} "
                        f"ndcg@10={metric.ndcg_at_10} {tag}",
                    )
    finally:
        await engine_manager.aclose_all()

    verdicts: list[dict[str, Any]] = []
    if args.judge == "on":
        llm = LLMClient(settings)
        if not llm.configured:
            print("LLM 未配置,跳过 judge。", file=sys.stderr)
        else:
            for mode in modes:
                verdicts.extend(
                    await _judge_via_replay(llm, cases, strategies, mode),
                )

    report = {
        "generated_at_perf_counter": time.perf_counter(),
        "fixture": str(fixture_path),
        "strategies": strategies,
        "modes": modes,
        "cases": len(cases),
        "runs": [
            {
                "case_id": metric.case_id,
                "strategy": metric.strategy,
                "mode": metric.mode,
                "latency_ms": metric.latency_ms,
                "hit_chunk_ids": metric.hit_chunk_ids,
                "recall@5": metric.recall_at_5,
                "recall@10": metric.recall_at_10,
                "mrr@10": metric.mrr_at_10,
                "ndcg@10": metric.ndcg_at_10,
                "engine_stats": metric.engine_stats,
                "error": metric.error,
            }
            for metric in metrics
        ],
        "aggregate": _aggregate(metrics),
        "judge": {
            "verdicts": verdicts,
            "summary": _judge_summary(verdicts),
        },
    }

    if args.out:
        out_path = Path(args.out).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n报告已写入 {out_path}")
    else:
        print("\n===== aggregate =====")
        print(json.dumps(report["aggregate"], ensure_ascii=False, indent=2))
        if verdicts:
            print("\n===== judge summary =====")
            print(json.dumps(report["judge"]["summary"], ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture",
        default="tests/fixtures/golden_queries.yaml",
        help="golden query 集 YAML 路径,默认相对 apps/api",
    )
    parser.add_argument(
        "--strategies",
        default="vector,multi_es_fast",
        help="逗号分隔的策略名,例:vector,multi_es_fast,multi",
    )
    parser.add_argument(
        "--mode",
        default="both",
        choices=["raw", "full", "both"],
        help="raw=裸引擎召回,full=完整流水线,both=两个都跑",
    )
    parser.add_argument(
        "--judge",
        default="off",
        choices=["on", "off"],
        help="打开 LLM-as-judge 打分",
    )
    parser.add_argument("--out", help="报告 JSON 输出路径;不传就打印到控制台")
    args = parser.parse_args()
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
