"""Public LiteLLM exceptions retain status through custom-provider dispatch."""

import httpx
import litellm


def provider_error(status: int, message: str):
    # Attach a synthetic request, never the actual key-bearing provider request.
    response = httpx.Response(status, request=httpx.Request("POST", "https://responses.invalid/responses"))
    common = {"message": message, "llm_provider": "sag_responses", "model": "sag_responses"}
    classes = {
        400: litellm.BadRequestError,
        401: litellm.AuthenticationError,
        403: litellm.PermissionDeniedError,
        404: litellm.NotFoundError,
        422: litellm.BadRequestError,
        429: litellm.RateLimitError,
    }
    if status == 408:
        error = litellm.Timeout(**common)
    elif status in classes:
        error = classes[status](**common, response=response)
    elif status in {500, 502, 503, 504}:
        error = litellm.ServiceUnavailableError(**common, response=response)
    else:
        error = litellm.APIError(status_code=status, **common, request=response.request)
    error.status_code = status
    return error
