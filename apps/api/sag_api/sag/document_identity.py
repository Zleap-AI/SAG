"""Business confirmation identities; the engine owns checkpoint eligibility."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from urllib.parse import quote, urlsplit

from sqlalchemy import or_, select
from sqlalchemy.exc import SQLAlchemyError
from zleap.sag import EngineConfig
from zleap.sag.core.adapters import registry
from zleap.sag.core.prompt.manager import PromptManager
from zleap.sag.db import EntityType
from zleap.sag.modules.extract.prompts import ExtractionPromptRegistry, ExtractionPromptRole
from zleap.sag.modules.extract.schema import extraction_response_schema
from zleap.sag.modules.load.chunking import BlockType, MarkdownBlockParser, MarkdownInputNormalizer

from sag_api.core.config import Settings
from sag_api.core.errors import ServiceUnavailableError
from sag_api.core.responses import Config as ResponsesConfig
from sag_api.parsing.service import document_parser_identity
from sag_api.sag.incremental_processor import document_extraction_options


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while block := file.read(64 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _input_identity(path: str) -> dict:
    original = Path(path)
    identity = {"format": original.suffix.lower(), "sha256": _file_digest(original)}
    if original.suffix.lower() not in {".md", ".markdown"}:
        return identity
    content = original.read_text(encoding="utf-8-sig")
    root = original.resolve().parent
    images: dict[str, str | None] = {}
    normalized = MarkdownInputNormalizer().normalize(content)
    for block in MarkdownBlockParser(parse_images=True).parse_blocks(normalized):
        if block.block_type != BlockType.IMAGE:
            continue
        reference = block.metadata["image_src"]
        target = Path(reference)
        if urlsplit(reference).scheme or target.is_absolute():
            continue
        resolved = (root / target).resolve()
        if resolved.is_relative_to(root):
            images[reference] = _file_digest(resolved)
    identity["images"] = images
    return identity


async def _entity_contract(config: EngineConfig, source_config_id: str) -> list[dict]:
    relational = config.relational
    if relational.provider == "sqlite":
        try:
            path = Path(relational.path).resolve(strict=True)
            if not path.is_file():
                raise OSError("not a file")
        except OSError:
            raise ServiceUnavailableError("处理配置暂不可读取，请在引擎存储可用后重试") from None
        relational = relational.model_copy(update={
            "path": "file:" + quote(path.as_posix(), safe="/") + "?mode=ro&uri=true",
        })
    store = registry.create_relational_store(relational.provider, config=relational)
    try:
        async with store.session() as session:
            rows = await session.scalars(select(EntityType).where(
                EntityType.is_active.is_(True),
                or_(EntityType.is_default.is_(True), EntityType.data_source_id == source_config_id),
            ).order_by(EntityType.is_default.desc()))
            contract: dict[str, dict] = {}
            for row in rows:
                contract.setdefault(row.type, {
                    "type": row.type, "name": row.name, "description": row.description or "",
                    "value_format": row.value_format, "value_constraints": row.value_constraints,
                })
            return [contract[key] for key in sorted(contract)]
    except SQLAlchemyError:
        raise ServiceUnavailableError("处理配置暂不可读取，请在引擎存储可用后重试") from None
    finally:
        await store.close()


async def _model_identities(config: EngineConfig) -> dict:
    llm = registry.create_llm(config=config.llm)
    try:
        embedding = registry.create_embedding(config=config.embedding)
        try:
            llm_identity, embedding_identity = llm.reuse_identity(), embedding.reuse_identity()
            if llm_identity is None or embedding_identity is None:
                raise ServiceUnavailableError("当前模型适配器未提供处理身份，无法确认重跑")
            return {"llm": asdict(llm_identity), "embedding": asdict(embedding_identity)}
        finally:
            await embedding.close()
    finally:
        await llm.close()


def _responses_identity(settings: Settings) -> dict | None:
    if settings.llm_provider != "responses":
        return None
    config = ResponsesConfig.from_settings(settings)
    return {
        "provider": config.provider, "endpoint": config.endpoint, "send_temperature": config.send_temperature,
        "thinking_rules": {model: dict(rule) for model, rule in config.thinking_rules.items()},
    }


async def document_processing_identity(
    config: EngineConfig, settings: Settings, source_config_id: str, path: str, *, engine_kind: str = "sag1",
) -> dict[str, str]:
    raw = await asyncio.to_thread(_input_identity, path)
    if engine_kind == "sag2":
        return {
            "configuration_digest": _digest({
                "engine_kind": "sag2", "parser": document_parser_identity(path, settings),
                "chunking": {"strategy": "standard", "max_tokens": settings.document_chunk_max_tokens},
                "storage": {"data_dir": str(Path(settings.data_dir).resolve()),
                            "relational_provider": settings.sag_relational_provider,
                            "pg_host": settings.sag_pg_host, "pg_port": settings.sag_pg_port,
                            "pg_database": settings.sag_pg_database, "pg_user": settings.sag_pg_user},
            }),
            "input_digest": _digest(raw), "original_sha256": raw["sha256"],
        }
    entity_contract = await _entity_contract(config, source_config_id)
    return {
        "configuration_digest": _digest({
            "llm": config.llm.model_dump(mode="json", exclude={"api_key", "timeout", "max_retries"}),
            "embedding": config.embedding.model_dump(mode="json", exclude={"api_key", "timeout", "max_retries"}),
            "language": config.language,
            "storage": {
                "data_dir": str(Path(config.data_dir).resolve()),
                "relational": config.relational.model_dump(mode="json", include={
                    "provider", "host", "port", "user", "database", "path",
                }),
                "vector": config.vector.model_dump(mode="json", exclude={
                    "password": True, "api_key": True, "connection": {"password": True},
                }),
            },
            "chunking": {"strategy": "standard", "max_tokens": settings.document_chunk_max_tokens},
            "max_entities_per_event": 8 if settings.document_extraction_profile == "concise" else 20,
            "extra_body": settings.llm_extra_body,
            "responses": _responses_identity(settings),
            "model_identities": await _model_identities(config),
            "prompt": asdict(ExtractionPromptRegistry.from_prompt_manager(
                PromptManager(language=config.language),
            ).load(ExtractionPromptRole.DOCUMENT)),
            "schema": extraction_response_schema("rich"),
            "provider_schema": extraction_response_schema("rich", for_provider=True),
            "extraction_options": document_extraction_options(
                config.language, max_concurrency=1,
                max_entities_per_event=8 if settings.document_extraction_profile == "concise" else 20,
            ).model_dump(mode="json", exclude={"execution"}),
            "entity_contract": entity_contract,
            "parser": document_parser_identity(path, settings),
        }),
        "input_digest": _digest(raw),
        "original_sha256": raw["sha256"],
    }


def processing_identity_restart_reason(saved: dict[str, str], current: dict[str, str]) -> str | None:
    if saved["configuration_digest"] != current["configuration_digest"]:
        return "resume_configuration_changed"
    if saved["input_digest"] != current["input_digest"]:
        return "resume_input_changed"
    return None
