"""错误分类维度（layer/stage）单元测试。

覆盖三个补齐点：
1. `map_sag_errors` 现在会拦截逃逸的 `jsonschema.ValidationError`（历史 minItems bug）。
2. LLM 厂商错误按超时/限流/鉴权/非法请求拆分，不再一律 UpstreamError。
3. 文档失败按当前状态推断 stage，责任层归 engine 兜底。
4. 错误信封携带 layer/stage/retryable/request_id。
"""

from __future__ import annotations

import pytest

from sag_api.core.error_taxonomy import ErrorLayer, ErrorStage
from sag_api.core.errors import (
    ApiError,
    ConfigurationError,
    ServiceUnavailableError,
    UpstreamError,
    ValidationError,
)
from sag_api.enums import DocumentStatus
from sag_api.generation.llm import _classify_llm_error
from sag_api.jobs.tasks import _classify_document_failure
from sag_api.sag.errors import map_sag_errors


def test_envelope_carries_all_dimensions():
    err = ValidationError("bad", layer=ErrorLayer.LLM, stage=ErrorStage.EXTRACT, retryable=False)
    env = err.to_envelope(request_id="abc123")["error"]
    assert env["code"] == "validation_error"
    assert env["layer"] == "llm"
    assert env["stage"] == "extract"
    assert env["retryable"] is False
    assert env["request_id"] == "abc123"


def test_envelope_omits_request_id_when_absent():
    assert "request_id" not in ApiError("x").to_envelope()["error"]


def test_configuration_error_defaults_to_config_stage():
    err = ConfigurationError("尚未配置 LLM")
    assert err.stage == ErrorStage.CONFIG
    assert err.layer == ErrorLayer.API


def test_map_sag_errors_catches_escaped_jsonschema_validation():
    """历史上 references 的 minItems 校验失败会漏成裸 Exception。"""
    jsonschema = pytest.importorskip("jsonschema")
    with pytest.raises(ValidationError) as exc:
        with map_sag_errors(stage=ErrorStage.EXTRACT):
            raise jsonschema.exceptions.ValidationError("[] should be non-empty (minItems)")
    err = exc.value
    assert err.code == "schema_validation_error"
    assert err.layer == ErrorLayer.LLM
    assert err.stage == ErrorStage.EXTRACT


def test_classify_llm_timeout_is_retryable():
    class APITimeoutError(Exception):
        pass

    err = _classify_llm_error(APITimeoutError("timed out"), stage=ErrorStage.GENERATE)
    assert isinstance(err, ServiceUnavailableError)
    assert err.retryable is True
    assert err.layer == ErrorLayer.LLM


def test_classify_llm_auth_becomes_config_error():
    class AuthenticationError(Exception):
        status_code = 401

    err = _classify_llm_error(AuthenticationError("bad key"), stage=ErrorStage.GENERATE)
    assert isinstance(err, ConfigurationError)
    assert err.stage == ErrorStage.CONFIG
    assert err.code == "llm_auth_error"


def test_classify_llm_generic_falls_back_to_upstream():
    err = _classify_llm_error(RuntimeError("weird"), stage=ErrorStage.GENERATE)
    assert isinstance(err, UpstreamError)
    assert err.layer == ErrorLayer.LLM
    assert err.stage == ErrorStage.GENERATE


def test_document_failure_trusts_apierror_dimensions():
    api_err = ValidationError("x", layer=ErrorLayer.LLM, stage=ErrorStage.EXTRACT)
    layer, stage = _classify_document_failure(api_err, DocumentStatus.LOADING)
    assert (layer, stage) == (ErrorLayer.LLM, ErrorStage.EXTRACT)


def test_document_failure_falls_back_to_status_stage():
    layer, stage = _classify_document_failure(RuntimeError("naked"), DocumentStatus.EXTRACTING)
    assert layer == ErrorLayer.ENGINE
    assert stage == ErrorStage.EXTRACT

    layer, stage = _classify_document_failure(RuntimeError("naked"), DocumentStatus.LOADING)
    assert stage == ErrorStage.PARSE
