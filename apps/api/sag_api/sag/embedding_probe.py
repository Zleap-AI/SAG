"""Probe the original embedding adapter and translate engine failures safely."""

from __future__ import annotations

import math
from typing import NotRequired, TypedDict

from zleap.sag.config import EmbeddingConfig
from zleap.sag.core.adapters.defaults import OpenAIEmbeddingAdapter
from zleap.sag.exceptions import AIAuthError, AIError, AIInvalidRequestError, AITransientError

from sag_api.core.config import Settings


class EmbeddingConnectionTestResult(TypedDict):
    ok: bool
    message: str
    code: NotRequired[str]
    dimensions: NotRequired[int]


async def probe_embedding_connection(configuration: Settings) -> EmbeddingConnectionTestResult:
    """Use the original connection, independently of optional query adapters."""
    if not configuration.effective_embedding_api_key:
        return {"ok": False, "message": "No embedding API key configured"}
    try:
        # Use the original adapter directly; query connections cannot replace this test.
        adapter = OpenAIEmbeddingAdapter(
            config=EmbeddingConfig(
                model=configuration.embedding_model,
                api_key=configuration.effective_embedding_api_key,
                base_url=configuration.effective_embedding_base_url,
                schema_dimensions=configuration.effective_embedding_schema_dimensions,
                request_dimensions=configuration.effective_embedding_request_dimensions,
                timeout=configuration.embedding_timeout,
            )
        )
        try:
            vector = await adapter.generate("SAG embedding connection test")
            if not all(math.isfinite(value) for value in vector):
                raise ValueError("Non-finite embedding vector")
        finally:
            await adapter.close()
    except AIAuthError:
        return {
            "ok": False,
            "code": "EMBEDDING_AUTH_FAILED",
            "message": "Embedding authentication failed; check the saved embedding key or enter a key for this card",
        }
    except AIInvalidRequestError:
        return {
            "ok": False,
            "code": "EMBEDDING_REQUEST_REJECTED",
            "message": "Embedding request rejected; check the endpoint, model and supported request dimensions",
        }
    except AITransientError as exc:
        timeout = exc.details.get("failure_kind") == "timeout"
        return {
            "ok": False,
            "code": "EMBEDDING_TIMEOUT" if timeout else "EMBEDDING_UNAVAILABLE",
            "message": "Embedding request timed out; check the endpoint and server availability"
            if timeout else "Embedding service unavailable; check the endpoint, rate limits and server availability",
        }
    except (AIError, ValueError):
        return {
            "ok": False,
            "code": "EMBEDDING_INVALID_RESPONSE",
            "message": (
                "Embedding returned an invalid response; check the returned vector format and configured dimensions"
            ),
        }
    except Exception:  # noqa: BLE001 -- provider bodies and credentials must stay private.
        return {
            "ok": False,
            "message": "Embedding test failed; check endpoint, model, credentials, dimensions and server availability",
        }
    return {
        "ok": True,
        "message": f"Embedding connection successful · {len(vector)} dimensions",
        "dimensions": len(vector),
    }
