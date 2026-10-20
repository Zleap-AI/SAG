"""Probe the same SDK scoring protocol used by JEV retrieval."""

from __future__ import annotations

import asyncio

from pydantic import BaseModel
from zleap.sag.core.ai.jev import JevClient
from zleap.sag.exceptions import AIAuthError, AIInvalidRequestError, AITransientError, LLMContextLengthError

from sag_api.core.config import Settings, normalize_jev_endpoint


class JevConnectionTestResult(BaseModel):
    ok: bool
    message: str


async def probe_jev_connection(configuration: Settings) -> JevConnectionTestResult:
    """Score synthetic evidence; never expose provider bodies, URLs or credentials."""
    try:
        endpoint = normalize_jev_endpoint(configuration.jev_endpoint)
    except (TypeError, ValueError):
        return JevConnectionTestResult(ok=False, message="请填写有效的 JEV 完整 HTTP(S) 服务地址")
    if not endpoint:
        return JevConnectionTestResult(ok=False, message="尚未配置 JEV 完整服务地址")
    if not configuration.jev_api_key:
        return JevConnectionTestResult(ok=False, message="请填写独立的 JEV API Key")
    if not configuration.jev_configured:
        return JevConnectionTestResult(ok=False, message="JEV 配置无效，请检查模型、API Key、评分超时和失败策略")
    client = JevClient(
        endpoint=endpoint,
        model=configuration.jev_model,
        api_key=configuration.jev_api_key,
        timeout=configuration.jev_ranking_timeout_seconds,
        max_retries=0,
    )
    try:
        await asyncio.wait_for(
            client.score(
                "In this synthetic example, where is Cedar's headquarters?",
                [
                    {"id": "0", "text": "In this synthetic example, Cedar is headquartered in Harbor City."},
                    {"id": "1", "text": "A triangle is a shape with three sides."},
                ],
            ),
            timeout=configuration.jev_ranking_timeout_seconds,
        )
        return JevConnectionTestResult(ok=True, message="JEV 连接成功")
    except AIAuthError:
        return JevConnectionTestResult(ok=False, message="JEV 凭据无效或没有权限")
    except LLMContextLengthError:
        return JevConnectionTestResult(ok=False, message="JEV 服务拒绝了测试请求：上下文超限")
    except AIInvalidRequestError:
        return JevConnectionTestResult(ok=False, message="JEV 模型或评分协议不受服务支持")
    except (TimeoutError, AITransientError):
        return JevConnectionTestResult(ok=False, message="JEV 连接超时、限流或服务暂时不可用")
    except Exception:  # noqa: BLE001 -- provider exception text may include private data.
        return JevConnectionTestResult(ok=False, message="JEV 连接测试失败，请检查服务地址和评分协议")
    finally:
        await client.aclose()
