from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_all,
    collect_data_files,
    collect_submodules,
    copy_metadata,
)

project_root = Path(SPECPATH).parent

datas = []
binaries = []
hiddenimports = [
    "aiosqlite",
    "sqlalchemy.dialects.sqlite",
    "sqlalchemy.dialects.sqlite.aiosqlite",
    "tiktoken_ext.openai_public",
]

def is_runtime_submodule(name):
    return ".tests" not in name and not name.startswith("onnxruntime.quantization")


for package in (
    "lancedb",
    "magika",
    "markitdown",
    "tiktoken",
    "tokenizers",
    "zleap",
):
    package_datas, package_binaries, package_imports = collect_all(
        package,
        filter_submodules=is_runtime_submodule,
    )
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_imports

# LiteLLM supports many providers and an optional proxy server. SAG only exposes
# OpenAI, Anthropic, and Gemini, so freezing every LiteLLM module would pull
# unrelated server/CLI dependencies into the desktop sidecar.
datas += collect_data_files("litellm")
for package in (
    "litellm.litellm_core_utils",
    "litellm.types",
    "litellm.llms.base_llm",
    "litellm.llms.custom_httpx",
    "litellm.llms.openai",
    "litellm.llms.anthropic",
    "litellm.llms.gemini",
):
    hiddenimports += collect_submodules(package)
hiddenimports += ["litellm.integrations.custom_logger"]

# The application uses MCP clients and FastMCP servers, but not mcp.cli. The CLI
# has optional Typer dependencies and should not become part of the app runtime.
datas += collect_data_files("mcp")
for package in (
    "mcp.client",
    "mcp.server",
    "mcp.shared",
    "mcp.os",
):
    hiddenimports += collect_submodules(package)

for package in (
    "sag-api",
    "zleap-sag",
    "lancedb",
    "litellm",
    "markitdown",
    "mcp",
):
    datas += copy_metadata(package, recursive=True)

hiddenimports += collect_submodules("sag_api")
hiddenimports += collect_submodules("sag_agent")

a = Analysis(
    [str(project_root / "sag_api" / "desktop.py")],
    pathex=[str(project_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "onnxruntime.backend",
        "onnxruntime.quantization",
        "onnxruntime.tools",
        "onnxruntime.transformers",
        "pip",
        "pyarrow.tests",
        "pytest",
        "ruff",
        "setuptools",
        "wheel",
    ],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="sag-api",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="sag-api",
)
