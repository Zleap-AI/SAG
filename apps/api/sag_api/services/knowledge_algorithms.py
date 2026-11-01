"""Business policy for immutable knowledge algorithms."""

from sag_api.core.errors import ValidationError


def require_sag1_octx(source) -> None:
    if getattr(source, "engine_kind", "sag1") == "sag2":
        raise ValidationError("证据检索（SAG2）知识库暂不支持 OCTX 导入覆盖或导出；OCTX 目前仅支持事件检索（SAG1）。")
