"""Apply official provider request limits without changing SDK embedding semantics."""

from urllib.parse import urlsplit


def _batch_limit(config):
    if config is None:
        return None
    host = urlsplit(config.base_url or "").hostname or ""
    if host == "open.bigmodel.cn" and config.model == "embedding-3":
        return 64
    if host == "dashscope.aliyuncs.com" or host.endswith(".cn-beijing.maas.aliyuncs.com"):
        return {"qwen3.7-text-embedding": 20, "text-embedding-v4": 10}.get(config.model)
    return None


class _BatchLimitedEmbeddingAdapter:
    def __init__(self, original, limit):
        self.original = original
        self.limit = limit

    def __getattr__(self, name):
        return getattr(self.original, name)

    async def batch_generate(self, texts):
        if len(texts) <= self.limit:
            return await self.original.batch_generate(texts)
        vectors = []
        for start in range(0, len(texts), self.limit):
            vectors.extend(await self.original.batch_generate(texts[start : start + self.limit]))
        return vectors


def with_embedding_batch_limit(adapter, config):
    """Wrap only recognized official endpoints; gateways retain their own limits."""
    limit = _batch_limit(config)
    return _BatchLimitedEmbeddingAdapter(adapter, limit) if limit is not None else adapter
