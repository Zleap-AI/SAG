from __future__ import annotations

from octx import ArchiveLimits

from sag_api.core.config import Settings

_MIB = 1024 * 1024


def build_archive_limits(settings: Settings) -> ArchiveLimits:
    """Convert deployment settings to the SDK's byte-based boundary exactly once."""
    return ArchiveLimits(
        max_entries=settings.octx_max_entries,
        max_file_size=settings.octx_max_file_mb * _MIB,
        max_total_uncompressed=settings.octx_max_uncompressed_mb * _MIB,
        max_compression_ratio=settings.octx_max_compression_ratio,
        max_jsonl_line_size=settings.octx_max_jsonl_line_mb * _MIB,
        max_jsonl_records=settings.octx_max_jsonl_records,
        max_issues=settings.octx_max_issues,
    )
