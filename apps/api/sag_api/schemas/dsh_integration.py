"""Public descriptions for a local DeepSeek Harness connection."""

from __future__ import annotations

from typing import Literal

from pydantic import AnyHttpUrl, BaseModel


class DshUploadCapability(BaseModel):
    """Document upload limits that a DSH client can present before uploading."""

    maxMb: int
    extensions: list[str]


class DshConnectionDescriptor(BaseModel):
    """Local-only connection settings, including the connector credential."""

    schemaVersion: Literal[1] = 1
    name: str
    apiUrl: AnyHttpUrl
    mcpUrl: AnyHttpUrl
    accessToken: str
    defaultSourceId: str | None = None


class DshCapabilityDescriptor(BaseModel):
    """Non-secret SAG operations exposed to a DeepSeek Harness profile."""

    schemaVersion: Literal[1] = 1
    capabilities: list[str]
    upload: DshUploadCapability
    defaultSourceId: str | None = None


class DshIntegrationUpdate(BaseModel):
    """Requested change to the persisted local DSH integration settings."""

    default_source_id: str | None = None
