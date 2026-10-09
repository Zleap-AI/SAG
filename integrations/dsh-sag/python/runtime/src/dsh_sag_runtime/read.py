"""Bounded evidence reader for the `sag_read` tool."""

from __future__ import annotations

from typing import Any

from .evidence import EvidenceRefCodec
from .protocol import ReadParams


class ReadAdapter:
    """Resolve an opaque reference and return one Unicode-safe content page."""

    def __init__(self, pool: Any, codec: EvidenceRefCodec, *, max_read_chars: int, max_events: int) -> None:
        self._pool = pool
        self._codec = codec
        self._max_read_chars = max_read_chars
        self._max_events = max_events

    async def read(self, params: ReadParams) -> dict[str, Any]:
        locator = self._codec.decode(params.evidence_ref)
        async with self._pool.read_engine(locator.namespace_id) as engine:
            records = await engine.read_evidence(
                locator.source_id,
                (locator.chunk_id,),
                max_events_per_chunk=self._max_events,
            )
        matches = tuple(record for record in records if record.chunk.id == locator.chunk_id)
        if len(matches) != 1:
            raise ValueError("evidence reference must resolve to exactly one chunk")
        record = matches[0]
        content = record.chunk.content
        if params.offset > len(content):
            raise ValueError("read offset is beyond the evidence content")
        page_size = min(params.max_chars, self._max_read_chars)
        page = content[params.offset : params.offset + page_size]
        next_offset = params.offset + len(page)
        result: dict[str, Any] = {
            "title": record.chunk.heading or record.chunk.source_id,
            "content": page,
            "offset": params.offset,
            "totalChars": len(content),
        }
        if next_offset < len(content):
            result["nextOffset"] = next_offset
        if params.include_events:
            result["events"] = [
                {
                    key: value
                    for key, value in {
                        "id": getattr(event, "id", None),
                        "title": getattr(event, "title", None),
                        "summary": getattr(event, "summary", None),
                        "category": getattr(event, "category", None),
                        "rank": getattr(event, "rank", None),
                    }.items()
                    if value is not None
                }
                for event in record.events[: self._max_events]
            ]
        return result
