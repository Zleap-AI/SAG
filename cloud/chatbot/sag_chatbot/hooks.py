"""Guarded import-time seams; supported adapter registration stays outside stock code."""
from __future__ import annotations

import hashlib
import importlib.abc
import importlib.machinery
import importlib.metadata
import json
import sys
from functools import wraps
from pathlib import Path

TARGETS = {
    "sag_api.core.litellm_policy", "sag_api.services.settings_service", "sag_api.runtime",
    "sag_api.sag.search_reader", "sag_api.services.agent_domain", "sag_api.api.v1.system",
}
_installed = False


def verify():
    manifest = json.loads(Path(__file__).with_name("compatibility.json").read_text())
    for module, expected in manifest["sources"].items():
        package, *relative = module.split(".")
        spec = importlib.machinery.PathFinder.find_spec(package)
        if not spec or not spec.submodule_search_locations:
            raise RuntimeError(f"Chatbot extension requires installed {package}")
        source = Path(next(iter(spec.submodule_search_locations))).joinpath(*relative).with_suffix(".py")
        if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != expected:
            raise RuntimeError(f"Chatbot compatibility changed at {module}; review cloud/chatbot/README.md and rerun upgrade tests")
    for package, versions in manifest["dependencies"].items():
        if importlib.metadata.version(package) not in versions:
            raise RuntimeError(f"Chatbot extension requires audited {package}; review cloud/chatbot/sag_chatbot/compatibility.json")


def patch(module):
    from . import runtime as rt

    if module.__name__ == "sag_api.services.settings_service":
        original = module.apply_startup_overrides

        @wraps(original)
        async def startup(session_factory):
            await original(session_factory)
            await rt.manager.load(session_factory)

        module.apply_startup_overrides = startup
    elif module.__name__ == "sag_api.sag.search_reader":
        # Cover direct and nested entry points, including bulk vector/event recall.
        for name in ("search", "search_many", "_search_raw", "_search_chunk_vectors", "search_event_scores"):
            original = getattr(module.SearchReader, name)

            def wrap(function):
                @wraps(function)
                async def scoped(self, *args, **kwargs):
                    async with rt.manager.scope(query=True):
                        return await function(self, *args, **kwargs)
                return scoped

            setattr(module.SearchReader, name, wrap(original))
    elif module.__name__ == "sag_api.services.agent_domain":
        # Only the history budget reads query settings; persistence/auth stay stock.
        module.settings = rt.SettingsView(module.settings, context_only=True)
        original = module.prepare_ask

        @wraps(original)
        async def prepare(*args, **kwargs):
            async with rt.manager.scope():
                return await original(*args, **kwargs)

        module.prepare_ask = prepare
    elif module.__name__ == "sag_api.api.v1.system":
        original = module._capabilities

        @wraps(original)
        def capabilities():
            result = original()
            snapshot = rt.operation.get() or rt.manager.snapshot()
            if snapshot.connections["llm"].enabled:
                result.update(
                    llm_configured=snapshot.settings.llm_configured,
                    # Keep the stock public ModelProviderId contract; Responses uses its OpenAI slot.
                    llm_provider=snapshot.settings.llm_provider,
                    llm_model=snapshot.settings.llm_model,
                    context_window=snapshot.settings.llm_context_window,
                )
            return result

        module._capabilities = capabilities
    elif module.__name__ == "sag_api.core.litellm_policy":
        original = module.apply_litellm_completion_policy
        standard_policy = original

        @wraps(original)
        def policy(settings, request):
            snapshot = rt.operation.get()
            separate = snapshot is not None and rt.query_scope.get() and snapshot.connections["llm"].enabled
            if not separate:
                return original(settings, request)
            settings = snapshot.settings
            # Dependency-supplied legacy options belong to its stock model, not the query provider.
            request = {**request, "extra_body": dict(settings.llm_extra_body or {})}
            if not snapshot.connections["llm"].api_key:
                from openai import omit

                from .config import KEYLESS_API_KEY
                request["api_key"] = KEYLESS_API_KEY
                if settings.llm_provider == "openai" and snapshot.responses_config is None:
                    request["extra_headers"] = {**(request.get("extra_headers") or {}), "Authorization": omit}
            from .config import PROVIDERS
            if not PROVIDERS[settings.llm_provider].temperature_configurable:
                request["temperature"] = settings.effective_llm_temperature
            if snapshot.responses_config is None:
                return standard_policy(settings, request)
            from sag_chatbot.responses.policy import normalize_thinking
            result = normalize_thinking(standard_policy(settings, request), snapshot.responses_config.thinking_rules)
            if result.get("stream"):
                result["stream_options"] = {**(result.get("stream_options") or {}), "include_usage": True}
            return result

        module.apply_litellm_completion_policy = policy
    elif module.__name__ == "sag_api.runtime":
        from .provider import QueryLLM

        module._RuntimeFactory.create_llm_client = lambda self, settings: QueryLLM(settings)
        original = module._RuntimeFactory.install_litellm_policy

        @wraps(original)
        def install_policy(self):
            return module.install_litellm_policy(rt.SettingsView(self.settings))

        module._RuntimeFactory.install_litellm_policy = install_policy


class Loader(importlib.abc.Loader):
    def __init__(self, original):
        self.original = original

    def create_module(self, spec):
        create = getattr(self.original, "create_module", None)
        return create(spec) if create else None

    def exec_module(self, module):
        self.original.exec_module(module)
        patch(module)


class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname not in TARGETS:
            return None
        # Compose with subsequent import loaders without bypassing their transforms.
        following = False
        for finder in sys.meta_path:
            if finder is self:
                following = True
                continue
            if not following or not hasattr(finder, "find_spec"):
                continue
            spec = finder.find_spec(fullname, path, target)
            if spec is not None:
                spec.loader = Loader(spec.loader)
                return spec
        raise RuntimeError(f"Chatbot extension cannot load {fullname}")


def install():
    global _installed
    if _installed:
        return
    if TARGETS.intersection(sys.modules):
        raise RuntimeError("Install chatbot bootstrap before importing SAG routes, runtime or retrieval")
    verify()
    from sag_api.core.config import settings

    from . import runtime as rt
    from .config import Environment

    rt.manager = rt.Manager(Environment(), settings)
    sys.meta_path.insert(0, Finder())
    # Original factories retain full ownership of extraction/indexing/import clients.
    from zleap.sag.core.adapters import defaults, registry
    registry.register("llm", "openai", lambda **kwargs: rt.ScopedAdapter(defaults.OpenAILLMAdapter(**kwargs), "llm"))
    registry.register("embedding", "openai", lambda **kwargs: rt.ScopedAdapter(defaults.OpenAIEmbeddingAdapter(**kwargs), "embedding"))
    from .provider import install_responses_route
    install_responses_route()
    # Capture one query snapshot and replay state before any runtime is constructed.
    from sag_agent.runtime import AgentRuntime
    original_drive = AgentRuntime._drive

    @wraps(original_drive)
    async def drive(self, *args, **kwargs):
        async with rt.manager.scope():
            state = {}
            token = rt.chat_replay.set(state)
            try:
                return await original_drive(self, *args, **kwargs)
            finally:
                state.clear()
                rt.chat_replay.reset(token)

    AgentRuntime._drive = drive
    _installed = True


def attach(app):
    if getattr(app.state, "chatbot_installed", False):
        return app
    from .api import router
    from .runtime import CaptureMiddleware
    app.include_router(router, prefix="/api/v1/system")
    app.add_middleware(CaptureMiddleware)
    app.state.chatbot_installed = True
    return app
