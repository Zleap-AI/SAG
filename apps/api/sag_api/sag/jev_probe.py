"""Translate the engine's JEV connection probe into an application result."""

from __future__ import annotations

import asyncio

from pydantic import BaseModel
from zleap.sag.core.ai.jev import JevClient
from zleap.sag.exceptions import AIAuthError, AIInvalidRequestError, AITransientError, LLMContextLengthError

from sag_api.core.config import Settings


class JevConnectionTestResult(BaseModel):
    ok: bool
    message: str


async def probe_jev_connection(configuration: Settings) -> JevConnectionTestResult:
    """Score synthetic evidence without persisting settings or exposing provider errors."""
    if not configuration.jev_endpoint:
        return JevConnectionTestResult(ok=False, message="尚未配置 JEV 完整服务地址")
    if not configuration.jev_api_key:
        # JEV 是付费服务；没有 Key 的探测既不会成功也不应发出请求。
        return JevConnectionTestResult(ok=False, message="JEV 为付费服务，请先填写 API Key")
    client = JevClient(
        endpoint=configuration.jev_endpoint,
        model=configuration.jev_model,
        api_key=configuration.jev_api_key or "",
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
        return JevConnectionTestResult(ok=True, message=f"JEV 连接成功 · {configuration.jev_model}")
    except AIAuthError:
        return JevConnectionTestResult(ok=False, message="JEV 凭据无效或没有权限")
    except LLMContextLengthError:
        return JevConnectionTestResult(ok=False, message="JEV 服务拒绝了测试请求：上下文超限")
    except AIInvalidRequestError:
        return JevConnectionTestResult(ok=False, message="JEV 模型或评分协议不受服务支持")
    except (TimeoutError, AITransientError):
        return JevConnectionTestResult(ok=False, message="JEV 连接超时、限流或服务暂时不可用")
    except Exception:  # noqa: BLE001 -- provider bodies and URLs can contain credentials.
        return JevConnectionTestResult(ok=False, message="JEV 连接测试失败，请检查服务地址和评分协议")
    finally:
        await client.aclose()
