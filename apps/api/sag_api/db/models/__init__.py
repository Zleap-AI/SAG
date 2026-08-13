"""ORM 模型聚合导入 —— 保证 Base.metadata 注册全部表。"""

from sag_api.db.models.agent import Agent, AgentBinding, Message, Thread
from sag_api.db.models.document import Document
from sag_api.db.models.job import Job
from sag_api.db.models.octx import (
    OctxAsset,
    OctxInstallation,
    OctxOperationLease,
    OctxRelease,
    OctxSourceBinding,
    OctxTransfer,
)
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
    "Job",
    "Message",
    "OctxAsset",
    "OctxInstallation",
    "OctxOperationLease",
    "OctxRelease",
    "OctxSourceBinding",
    "OctxTransfer",
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
