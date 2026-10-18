"""Run the published JEV profile and hydration against real offline 0.15 storage."""

import importlib.metadata
import json
import re
from uuid import uuid4

import httpx
import pytest
from litellm import ModelResponse
from openai import AsyncOpenAI
from zleap.sag import DataEngine

from sag_api.core.chatbot_config import Environment
from sag_api.core.config import Settings
from sag_api.sag.engine_manager import EngineManager
from sag_api.services import chatbot_service as rt
from sag_api.services.retrieval_service import retrieve_relevant_sections
from tests.document_requests import process_test_input


@pytest.mark.asyncio
async def test_real_015_jev_profile_scores_hydrated_published_evidence(
    tmp_path, monkeypatch, publication_source, published_inputs,
):
    assert importlib.metadata.version("zleap-sag") == "0.15.0"
    stock = Settings(
        _env_file=None,
        data_dir=str(tmp_path / "engine"),
        sag_vector_provider="lancedb",
        sag_relational_provider=None,
        sag_language="en",
        llm_provider="openai",
        llm_model="synthetic-extractor",
        llm_api_key="synthetic-extraction-key",
        llm_base_url="https://extractor.invalid/v1",
        embedding_model="synthetic-embedding",
        embedding_api_key="synthetic-embedding-key",
        embedding_base_url="https://embedding.invalid/v1",
        embedding_schema_dimensions=16,
        embedding_request_dimensions=16,
        jev_endpoint="https://scorer.invalid/custom/full-score-path",
        jev_model="synthetic-jev-model",
        jev_api_key="synthetic-jev-key",
        search_strategy="multi_jev",
        search_top_k=2,
    )
    monkeypatch.setattr(rt, "manager", rt.Manager(Environment({}), stock))
    documents = {
        "CEDAR_HISTORY": ("Cedar history", "CEDAR_HISTORY: Cedar was founded in 1998."),
        "CEDAR_HQ": ("Cedar headquarters", "CEDAR_HQ: Cedar is headquartered in Harbor City."),
        "UNPUBLISHED_ONLY": ("Cedar draft", "UNPUBLISHED_ONLY: A draft places Cedar in Draft City."),
        "OTHER_SOURCE_ONLY": ("Cedar private", "OTHER_SOURCE_ONLY: A private Cedar exception."),
    }
    embedding_calls, extraction_calls, scoring_calls = [], [], []

    def embed(request):
        assert request.url.host == "embedding.invalid"
        body = json.loads(request.content)
        inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
        embedding_calls.extend(inputs)
        # Equal recall scores force the final ordering to come from JEV.
        return httpx.Response(200, json={
            "data": [{"index": i, "embedding": [1.0] + [0.0] * 15} for i in range(len(inputs))],
            "model": body["model"],
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        })

    monkeypatch.setattr(
        "openai.AsyncOpenAI",
        lambda **kwargs: AsyncOpenAI(
            **kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(embed)),
        ),
    )

    async def extract(**kwargs):
        assert kwargs["api_base"] == stock.llm_base_url
        messages = json.dumps(kwargs["messages"])
        marker = next(marker for marker in documents if marker in messages)
        extraction_calls.append(marker)
        title, content = documents[marker]
        return ModelResponse(model=kwargs["model"], choices=[{
            "message": {"role": "assistant", "content": json.dumps({
                "type": "response",
                "data": {"items": [{
                    "reason": "The document states a reusable fact about Cedar.",
                    "title": title,
                    "summary": content,
                    "content": content,
                    "references": [1],
                    "entities": [{"type": "organization", "name": "Cedar", "description": "Company"}],
                }]},
            })},
            "finish_reason": "stop",
        }])

    monkeypatch.setattr("litellm.acompletion", extract)

    class ScoringSession:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def post(self, endpoint, *, data, headers, allow_redirects):
            payload = json.loads(data)
            assert endpoint == stock.jev_endpoint
            assert headers["Authorization"] == f"Bearer {stock.jev_api_key}"
            assert allow_redirects is False
            assert payload["model"] == stock.jev_model
            assert "UNPUBLISHED_ONLY" not in payload["state"]
            assert "OTHER_SOURCE_ONLY" not in payload["state"]
            assert set(payload["questions"]) == {"0", "1"}
            answers = {}
            for key, question in payload["questions"].items():
                assert question["type"] == "noul" and question["instructions"]
                candidate = re.search(rf"(?ms)^{re.escape(key)}: (.*?)(?=^\d+: |\Z)", payload["state"])
                assert candidate is not None
                text = candidate.group(1)
                assert "CEDAR_HQ" in text or "CEDAR_HISTORY" in text
                answers[key] = {"type": "noul", "noul": 0.87 if "CEDAR_HQ" in text else 0.24}
            scoring_calls.append(payload)

            class Response:
                status = 200
                headers = {}

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *_args):
                    return None

                async def json(self, **_kwargs):
                    return {
                        "model": payload["model"], "usage": {"input_tokens": 20, "output_tokens": 2},
                        "answers": answers,
                    }

            return Response()

    monkeypatch.setattr("zleap.sag.core.ai.jev.aiohttp.ClientSession", ScoringSession)
    manager = EngineManager(stock)
    source = await publication_source("Cedar public", f"jev-public-{uuid4().hex[:12]}")
    private_config = f"jev-private-{uuid4().hex[:12]}"
    published_ids = []
    try:
        for marker, (title, content) in documents.items():
            path = tmp_path / f"{marker}.md"
            path.write_text(f"# {title}\n\n{content}")
            config_id = private_config if marker == "OTHER_SOURCE_ONLY" else source.sag_source_config_id
            seeded = await process_test_input(manager, config_id, path)
            assert seeded.status == "succeeded"
            assert seeded.result["event_ref"]["event_ids"]
            if marker.startswith("CEDAR_"):
                published_ids.append(seeded.result["chunk_ref"]["source_id"])
        await published_inputs(source.id, *published_ids)
        assert isinstance(manager._slots[source.sag_source_config_id].engine, DataEngine)

        outcome = await retrieve_relevant_sections(
            manager, [source], "Where is Cedar headquartered and when was it founded?", top_k=2,
        )
        assert len(scoring_calls) == 1 and len(scoring_calls[0]["questions"]) == 2
        assert embedding_calls and set(extraction_calls) == set(documents)
        assert len(outcome.sections) == 2
        assert [section.score for section in outcome.sections] == [0.87, 0.24]
        assert "CEDAR_HQ" in outcome.sections[0].content
        assert "CEDAR_HISTORY" in outcome.sections[1].content
        assert all(section.metadata["score_source"] == "jev" for section in outcome.sections)
        assert all(section.metadata["event_ids"] for section in outcome.sections)
        assert all(section.source_id in published_ids for section in outcome.sections)
        assert all(section.source_config_id == source.sag_source_config_id for section in outcome.sections)
        assert outcome.stats["fallback_used"] is False
        strategy = outcome.stats["source_stats"][source.sag_source_config_id]["strategy"]
        assert strategy["resolved"] == "pruned_expand_jev" and strategy["executor"] == "production"
        assert outcome.stats["ranking"] == {"method": "jev", "fallback_reason": None}

        limited = await retrieve_relevant_sections(
            manager, [source], "Where is Cedar headquartered and when was it founded?", top_k=1,
        )
        assert len(scoring_calls) == 2
        assert len(limited.sections) == 1
        assert limited.sections[0].chunk_id == outcome.sections[0].chunk_id
        assert limited.sections[0].score == 0.87
    finally:
        await manager.aclose_all()
