"""sag 领域异常 —— 与框架无关，路由层统一映射为 HTTP 响应。

领域服务只抛这些异常；`sag/` 适配层负责把 `zleap-sag` 的 `SagError` 家族翻译到这里。

每个异常带三个维度：
- ``code``：HTTP 语义错误码（历史字段，向后兼容，前端逻辑沿用）。
- ``layer``：责任归属（谁该排查），见 :class:`ErrorLayer`。
- ``stage``：链路环节（哪一步崩），见 :class:`ErrorStage`。
另有 ``retryable`` 标识是否可安全重试。layer/stage/retryable 可在构造时按
实际发生点覆盖 —— 同一个 ``ValidationError`` 在 extract 阶段和 upload 阶段
应打上不同的 stage。
"""

from __future__ import annotations

from sag_api.core.error_taxonomy import ErrorCode, ErrorLayer, ErrorStage


class ApiError(Exception):
    """所有 sag 领域异常的基类。"""

    status_code: int = 500
    code: str = ErrorCode.INTERNAL_ERROR
    layer: ErrorLayer = ErrorLayer.API
    stage: ErrorStage = ErrorStage.UNKNOWN
    retryable: bool = False

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        layer: ErrorLayer | None = None,
        stage: ErrorStage | None = None,
        retryable: bool | None = None,
    ):
        self.message = message or self.__class__.__doc__ or "Internal error"
        if code:
            self.code = code
        if layer is not None:
            self.layer = layer
        if stage is not None:
            self.stage = stage
        if retryable is not None:
            self.retryable = retryable
        super().__init__(self.message)

    def to_envelope(self, *, request_id: str | None = None) -> dict:
        """序列化为响应/日志用的结构化错误信封。"""
        error: dict[str, object] = {
            "code": self.code,
            "message": self.message,
            "layer": self.layer.value,
            "stage": self.stage.value,
            "retryable": self.retryable,
        }
        if request_id:
            error["request_id"] = request_id
        return {"error": error}


class NotFoundError(ApiError):
    """请求的资源不存在。"""

    status_code = 404
    code = ErrorCode.NOT_FOUND


class ConflictError(ApiError):
    """资源冲突（如重复创建）。"""

    status_code = 409
    code = ErrorCode.CONFLICT


class ValidationError(ApiError):
    """输入校验失败。"""

    status_code = 422
    code = ErrorCode.VALIDATION_ERROR


class AuthError(ApiError):
    """未认证或凭证无效。"""

    status_code = 401
    code = ErrorCode.UNAUTHORIZED
    layer = ErrorLayer.API
    stage = ErrorStage.AUTH


class ForbiddenError(ApiError):
    """无权访问该资源。"""

    status_code = 403
    code = ErrorCode.FORBIDDEN
    layer = ErrorLayer.API
    stage = ErrorStage.AUTH


class ConfigurationError(ApiError):
    """缺少必要配置（如未配置 LLM）。"""

    status_code = 400
    code = ErrorCode.CONFIGURATION_ERROR
    layer = ErrorLayer.API
    stage = ErrorStage.CONFIG


class UpstreamError(ApiError):
    """上游（LLM / 引擎）返回错误。"""

    status_code = 502
    code = ErrorCode.UPSTREAM_ERROR
    layer = ErrorLayer.LLM


class ServiceUnavailableError(ApiError):
    """暂时不可用（可重试，如限流 / 超时）。"""

    status_code = 503
    code = ErrorCode.SERVICE_UNAVAILABLE
    retryable = True
