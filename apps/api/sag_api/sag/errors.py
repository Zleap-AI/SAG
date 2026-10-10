"""把 zleap-sag 的 `SagError` 家族翻译为 sag 领域异常。"""

from __future__ import annotations

from contextlib import contextmanager

from zleap.sag.exceptions import (
    ConfigError,
    InvalidInputError,
    NonRetryableError,
    OperationConflictError,
    ResourceNotFoundError,
    RetryableError,
    SagError,
)
from zleap.sag.operations import OperationResult

from sag_api.core.error_taxonomy import ErrorCode, ErrorLayer, ErrorStage
from sag_api.core.errors import (
    ConfigurationError,
    DocumentRestartRequiredError,
    NotFoundError,
    ServiceUnavailableError,
    UpstreamError,
    ValidationError,
)

try:  # jsonschema 是 zleap-sag structured-output 校验的传递依赖
    from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
except Exception:  # noqa: BLE001 - 依赖缺失时退化为不可能匹配的哨兵
    JsonSchemaValidationError = ()  # type: ignore[assignment]


def operation_failure(result: OperationResult, *, stage: ErrorStage) -> UpstreamError:
    """Preserve the SDK's structured failure at the existing business boundary."""
    return UpstreamError(
        result.failure_message or "引擎持久操作未完成",
        code=result.failure_code,
        layer=ErrorLayer.ENGINE,
        stage=stage,
        retryable=result.status in {"accepted", "running"} or bool(result.retryable),
    )


@contextmanager
def map_sag_errors(*, stage: ErrorStage = ErrorStage.UNKNOWN):
    """在此上下文内发生的引擎异常会被翻译成带 layer/stage 的 ApiError。

    ``stage`` 由调用方按当前链路环节传入（如 process_document 主要覆盖
    extract，search 覆盖 retrieve），使翻译出的错误携带准确的环节标记。
    """
    try:
        yield
    except ConfigError as e:
        raise ConfigurationError(str(e), layer=ErrorLayer.ENGINE, stage=stage) from e
    except ResourceNotFoundError as e:
        raise NotFoundError(str(e), layer=ErrorLayer.ENGINE, stage=stage) from e
    except InvalidInputError as e:
        raise ValidationError(str(e), layer=ErrorLayer.ENGINE, stage=stage) from e
    except RetryableError as e:
        # 限流 / 超时 / 上游暂不可用 —— 可重试
        raise ServiceUnavailableError(str(e), layer=ErrorLayer.ENGINE, stage=stage) from e
    except NonRetryableError as e:
        reason = e.details.get("reason")
        if (
            isinstance(e, OperationConflictError)
            and isinstance(reason, str)
            and reason in {
                "resume_configuration_changed", "resume_input_changed", "resume_input_unavailable",
                "resume_extraction_changed", "resume_artifacts_changed",
            }
        ):
            raise DocumentRestartRequiredError(reason, layer=ErrorLayer.ENGINE, stage=stage) from e
        raise ValidationError(str(e), layer=ErrorLayer.ENGINE, stage=stage) from e
    except JsonSchemaValidationError as e:  # type: ignore[misc]
        # structured-output schema 校验失败（如 references 的 minItems）：
        # 这类不属于 SagError 家族，历史上会漏出为裸 Exception。根子在
        # 模型没按 schema 输出 → 归到 LLM 层，环节沿用调用方传入的 stage。
        message = getattr(e, "message", None) or str(e)
        raise ValidationError(
            f"模型输出不符合结构化 schema：{message}",
            code=ErrorCode.SCHEMA_VALIDATION_ERROR,
            layer=ErrorLayer.LLM,
            stage=stage,
        ) from e
    except SagError as e:
        raise UpstreamError(
            str(e),
            layer=ErrorLayer.ENGINE,
            stage=stage,
            retryable=e.retryable,
        ) from e
