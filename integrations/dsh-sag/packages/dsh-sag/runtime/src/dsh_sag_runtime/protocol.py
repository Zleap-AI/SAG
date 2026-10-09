"""Versioned JSON-RPC 2.0 wire messages used over stdio."""

from __future__ import annotations

import json
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator

RPC_PROTOCOL_VERSION = "1.0"
REQUIRED_ENGINE_VERSION = "0.14.0"
MAX_FRAME_BYTES = 8 * 1024 * 1024


class WireModel(BaseModel):
    """Immutable strict wire model with camel-case serialization aliases."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class InitializeParams(WireModel):
    protocol_version: Literal["1.0"] = Field(alias="protocolVersion")


class SearchParams(WireModel):
    query: str = Field(min_length=1, max_length=32_000)
    namespaces: tuple[str, ...] = Field(min_length=1)
    mode: Literal["fast", "precise"]
    limit: int = Field(ge=1, le=50)

    @field_validator("namespaces")
    @classmethod
    def validate_namespaces(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value or len(value) > 36 for value in values):
            raise ValueError("namespace ids must contain 1 to 36 characters")
        return values


class ReadParams(WireModel):
    evidence_ref: str = Field(alias="evidenceRef", min_length=1, max_length=4096)
    offset: int = Field(ge=0)
    max_chars: int = Field(alias="maxChars", ge=1, le=200_000)
    include_events: bool = Field(alias="includeEvents")


class CancelParams(WireModel):
    id: int = Field(ge=0)


class EmptyParams(WireModel):
    pass


class InitializeRequest(WireModel):
    jsonrpc: Literal["2.0"]
    id: int = Field(ge=0)
    method: Literal["initialize"]
    params: InitializeParams


class SearchRequest(WireModel):
    jsonrpc: Literal["2.0"]
    id: int = Field(ge=0)
    method: Literal["search"]
    params: SearchParams


class ReadRequest(WireModel):
    jsonrpc: Literal["2.0"]
    id: int = Field(ge=0)
    method: Literal["read"]
    params: ReadParams


class ShutdownRequest(WireModel):
    jsonrpc: Literal["2.0"]
    id: int = Field(ge=0)
    method: Literal["shutdown"]
    params: EmptyParams


class CancelRequest(WireModel):
    jsonrpc: Literal["2.0"]
    method: Literal["$/cancelRequest"]
    params: CancelParams


RpcRequest: TypeAlias = InitializeRequest | SearchRequest | ReadRequest | ShutdownRequest | CancelRequest

_REQUEST_MODELS: dict[str, type[WireModel]] = {
    "initialize": InitializeRequest,
    "search": SearchRequest,
    "read": ReadRequest,
    "shutdown": ShutdownRequest,
    "$/cancelRequest": CancelRequest,
}


class RpcErrorData(WireModel):
    code: str
    operation: str | None = None
    stage: str | None = None
    retryable: bool | None = None
    provider: str | None = None
    item_id: str | None = Field(default=None, alias="itemId")
    message: str | None = None
    details: dict[str, Any] | None = None


class RpcError(WireModel):
    code: int
    message: str
    data: RpcErrorData | None = None


class RpcSuccessResponse(WireModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: int = Field(ge=0)
    result: dict[str, Any]


class RpcErrorResponse(WireModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: int = Field(ge=0)
    error: RpcError


RpcResponse: TypeAlias = RpcSuccessResponse | RpcErrorResponse


def decode_request(frame: bytes) -> RpcRequest:
    """Decode one bounded request frame and reject unknown methods or fields."""
    if len(frame) > MAX_FRAME_BYTES:
        raise ValueError("request frame exceeds 8 MiB")
    raw = json.loads(frame)
    if not isinstance(raw, dict):
        raise ValueError("request must be an object")
    method = raw.get("method")
    model = _REQUEST_MODELS.get(method) if isinstance(method, str) else None
    if model is None:
        raise ValueError("unsupported JSON-RPC method")
    return model.model_validate(raw)  # type: ignore[return-value]


def success_response(request_id: int, result: dict[str, Any]) -> RpcSuccessResponse:
    """Create a successful correlated response."""
    return RpcSuccessResponse(id=request_id, result=result)


def error_response(request_id: int, code: int, message: str, data: RpcErrorData) -> RpcErrorResponse:
    """Create a failed correlated response with safe structured data."""
    return RpcErrorResponse(id=request_id, error=RpcError(code=code, message=message, data=data))


def encode_response(response: RpcResponse) -> bytes:
    """Serialize one response as a compact NDJSON frame."""
    payload = response.model_dump_json(by_alias=True, exclude_none=True)
    frame = f"{payload}\n".encode()
    if len(frame) > MAX_FRAME_BYTES:
        raise ValueError("response frame exceeds 8 MiB")
    return frame
