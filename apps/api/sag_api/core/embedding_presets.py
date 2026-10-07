"""Embedding connections offered by first-run setup."""

from typing import Literal

EmbeddingSetupProvider = Literal["302", "zhipu", "bailian"]

QUICK_SETUP_EMBEDDINGS = {
    "302": {
        "embedding_model": "Qwen/Qwen3-Embedding-4B",
        "embedding_base_url": "https://api.302ai.cn/v1",
        "embedding_dimensions": 1024,
    },
    "zhipu": {
        "embedding_model": "embedding-3",
        "embedding_base_url": "https://open.bigmodel.cn/api/paas/v4",
        "embedding_dimensions": 1024,
    },
    "bailian": {
        "embedding_model": "text-embedding-v4",
        "embedding_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "embedding_dimensions": 1024,
    },
}
