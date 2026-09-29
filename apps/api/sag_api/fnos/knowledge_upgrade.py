"""One-time Native knowledge reset; old engine and originals remain recoverable."""

from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.db.models import (
    AgentBinding,
    Document,
    ExplorationSession,
    ExplorationStep,
    Job,
    Message,
    Setting,
    Source,
    UniverseDirtySource,
    UniverseOverview,
    UniversePartition,
)
from sag_api.enums import BindingTargetType

_UPGRADE_KEY = "fnos_knowledge_engine_0_13"


def stale_internal_citations(citations: list) -> list:
    return [
        {**citation, "stale": True} if isinstance(citation, dict) and citation.get("kind") != "external" else citation
        for citation in citations
    ]


async def reset_legacy_knowledge(session: AsyncSession, legacy_engine_dir: Path) -> int:
    """Clear old knowledge in one transaction before workers or engines start.

    A separate engine-v0.13-clean directory isolates the new index. The old
    engines/uploads stay untouched; the package gate verifies a cold backup of
    the complete tenant metadata before allowing install or upgrade to proceed.
    """
    marker = await session.scalar(select(Setting).where(Setting.scope == "global", Setting.key == _UPGRADE_KEY))
    if marker is not None and marker.value.get("knowledge_reset") is True:
        return 0
    if legacy_engine_dir.is_symlink():
        raise ValueError("legacy engine directory must not be a symlink")
    old_workspace = (
        marker is not None or legacy_engine_dir.is_dir() or bool(await session.scalar(select(func.count(Source.id))))
    )
    count = 0
    if old_workspace:
        count = await session.scalar(select(func.count(Document.id))) or 0
        for message in (await session.scalars(select(Message))).all():
            if isinstance(message.citations, list) and message.citations:
                message.citations = stale_internal_citations(message.citations)
        await session.execute(delete(AgentBinding).where(AgentBinding.target_type == BindingTargetType.SOURCE))
        # Delete dependents explicitly, including SQLite fixtures without FK pragmas.
        for model in (
            Job,
            ExplorationStep,
            ExplorationSession,
            UniversePartition,
            UniverseDirtySource,
            UniverseOverview,
            Document,
            Source,
        ):
            await session.execute(delete(model))
    value = {"engine": "0.13.0", "knowledge_reset": True, "legacy_retained": old_workspace}
    if marker is None:
        session.add(Setting(scope="global", key=_UPGRADE_KEY, value=value))
    else:
        marker.value = value
    await session.commit()
    return count
