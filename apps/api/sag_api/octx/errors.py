from __future__ import annotations

from octx.errors import (
    DerivationRequired,
    OctxFormatError,
    OctxIntegrityError,
    OctxOpenError,
    OctxResourceLimitError,
    OctxSecurityError,
    OctxValidationError,
    OutputExistsError,
    ReleaseVersionError,
)

from sag_api.core.error_taxonomy import ErrorCode, ErrorLayer, ErrorStage
from sag_api.core.errors import ApiError, ConflictError, ValidationError


class OctxSourceReextractRequiredError(ValidationError):
    """Stored Events are incomplete and their owning documents need re-extraction."""

    def __init__(self, documents: list[dict], *, event_count: int):
        super().__init__(
            "部分文档的事项数据不完整，无法导出。请重新提取这些文档后再试。",
            code=ErrorCode.OCTX_SOURCE_REEXTRACT_REQUIRED,
            layer=ErrorLayer.ENGINE,
            stage=ErrorStage.OCTX_EXPORT,
            retryable=False,
        )
        self.details = {
            "documents": documents,
            "event_count": event_count,
            "recovery_action": "reprocess_documents",
        }


def map_octx_error(error: Exception, stage: ErrorStage) -> ApiError:
    if isinstance(error, OctxResourceLimitError):
        return ValidationError(
            str(error),
            code=ErrorCode.OCTX_RESOURCE_LIMIT,
            layer=ErrorLayer.API,
            stage=stage,
            retryable=False,
        )
    if isinstance(error, (ReleaseVersionError, OutputExistsError, DerivationRequired)):
        return ConflictError(str(error), layer=ErrorLayer.API, stage=stage, retryable=False)
    if isinstance(
        error,
        (
            OctxFormatError,
            OctxSecurityError,
            OctxIntegrityError,
            OctxValidationError,
            OctxOpenError,
        ),
    ):
        return ValidationError(
            str(error),
            code=ErrorCode.OCTX_INVALID_PACKAGE,
            layer=ErrorLayer.API,
            stage=stage,
            retryable=False,
        )
    return ApiError(str(error), layer=ErrorLayer.API, stage=stage, retryable=False)
