"""Search Contract 2.0 adapter for the `sag_search` tool."""

from __future__ import annotations

from typing import Any

from zleap.sag.pipeline import SearchOptions, SearchRequest, SearchScope

from .evidence import EvidenceRefCodec
from .protocol import SearchParams

MODE_STRATEGY = {"fast": "vector", "precise": "full_expand"}


class SearchAdapter:
    """Map the stable sidecar request into zleap-sag 0.14.0 search types."""

    def __init__(
        self,
        pool: Any,
        codec: EvidenceRefCodec,
        namespaces: set[str] | frozenset[str],
        *,
        max_results: int,
        max_excerpt_chars: int,
    ) -> None:
        self._pool = pool
        self._codec = codec
        self._namespaces = frozenset(namespaces)
        self._max_results = max_results
        self._max_excerpt_chars = max_excerpt_chars

    async def search(self, params: SearchParams) -> dict[str, Any]:
        if any(namespace not in self._namespaces for namespace in params.namespaces):
            raise ValueError("search namespace is not configured")
        limit = min(params.limit, self._max_results)
        request = SearchRequest(
            query=params.query,
            scope=SearchScope(data_source_ids=params.namespaces),
            options=SearchOptions(
                strategy=MODE_STRATEGY[params.mode],
                top_k=limit,
                return_type="chunk",
            ),
        )
        result = await (await self._pool.search_engine()).search(request)
        chunk_hits = bool(result.chunks)
        hits = result.chunks or tuple(hit for hit in result.events if hit.chunk_id)
        evidences: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for hit in hits:
            if len(evidences) >= limit:
                break
            namespace_id = hit.data_source_id
            source_id = hit.source_id
            chunk_id = hit.chunk_id or (hit.id if chunk_hits else None)
            if not namespace_id or not source_id or not chunk_id:
                raise ValueError("SAG search hit does not contain readable identifiers")
            key = (namespace_id, source_id, chunk_id)
            if key in seen:
                continue
            seen.add(key)
            chunk = hit.chunk[0] if hit.chunk else None
            content = hit.content or (chunk.content if chunk is not None else "")
            title = hit.title or (chunk.heading if chunk is not None else "") or source_id
            evidence: dict[str, Any] = {
                "evidenceRef": self._codec.encode_from_parts(namespace_id, source_id, chunk_id),
                "namespaceId": namespace_id,
                "sourceId": source_id,
                "title": title,
                "excerpt": content[: self._max_excerpt_chars],
            }
            if hit.score is not None:
                evidence["score"] = hit.score
            evidences.append(evidence)
        return {"query": params.query, "evidences": evidences}
