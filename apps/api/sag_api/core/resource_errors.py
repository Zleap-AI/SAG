"""Recognize local descriptor exhaustion without parsing error messages."""

import errno
from collections.abc import Iterator

from sag_api.core.error_taxonomy import ErrorCode, ErrorLayer, ErrorStage
from sag_api.core.errors import ServiceUnavailableError


def exception_chain(error: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)


def descriptor_exhausted(error: BaseException) -> bool:
    return any(
        isinstance(cause, OSError) and cause.errno in {errno.EMFILE, errno.ENFILE}
        for cause in exception_chain(error)
    )


def resource_unavailable(
    *, stage: ErrorStage = ErrorStage.UNKNOWN, layer: ErrorLayer = ErrorLayer.API,
    code: str = ErrorCode.RESOURCE_EXHAUSTED, retryable: bool = True,
) -> ServiceUnavailableError:
    return ServiceUnavailableError(
        "服务器资源暂时不足，请稍后重试；如持续发生，请联系管理员",
        code=code, layer=layer, stage=stage, retryable=retryable,
    )
