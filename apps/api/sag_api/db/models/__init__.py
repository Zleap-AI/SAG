"""ORM 模型聚合导入 —— 保证 Base.metadata 注册全部表。"""

from sag_api.db.models.agent import Agent, AgentBinding, Message, Thread
from sag_api.db.models.document import Document
from sag_api.db.models.fnos_nas import FnOSNasLegacyFolder
from sag_api.db.models.job import Job
from sag_api.db.models.setting import Setting
from sag_api.db.models.source import Source
from sag_api.db.models.universe import (
    ExplorationSession,
    ExplorationStep,
    UniverseDirtySource,
    UniverseOverview,
    UniversePartition,
)
from sag_api.db.models.user import User

__all__ = [
    "Agent",
    "AgentBinding",
    "Document",
    "FnOSNasLegacyFolder",
    "Job",
    "Message",
    "Setting",
    "Source",
    "Thread",
    "User",
    "ExplorationSession",
    "ExplorationStep",
    "UniverseDirtySource",
    "UniverseOverview",
    "UniversePartition",
]
