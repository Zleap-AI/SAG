import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sag_chatbot import hooks

STOCK_WEB = Path(os.environ["SAG_TEST_STOCK_WEB"]) if "SAG_TEST_STOCK_WEB" in os.environ else next(
    parent / "apps/web" for parent in Path(__file__).resolve().parents if (parent / "apps/web").is_dir()
)


def test_guard_covers_wrapped_seams_and_changed_source_refuses_startup(tmp_path, monkeypatch):
    hooks.verify()
    manifest = json.loads(Path(hooks.__file__).with_name("compatibility.json").read_text())
    assert hooks.TARGETS | {"sag_agent.runtime"} <= manifest["sources"].keys()
    fake = tmp_path / "sag_api/core"
    fake.mkdir(parents=True)
    (fake.parent / "__init__.py").write_text("")
    (fake / "config.py").write_text("# incompatible upstream\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(RuntimeError, match="Chatbot compatibility changed at sag_api.core.config"):
        hooks.verify()


def test_web_overlay_preserves_form_and_refuses_changed_source(tmp_path):
    source = STOCK_WEB / "components/features/model-config-form.tsx"
    web = Path(__file__).resolve().parents[1] / "web"
    manifest = json.loads((web / "compatibility.json").read_text())
    assert manifest[source.relative_to(STOCK_WEB).as_posix()] == hashlib.sha256(source.read_bytes()).hexdigest()
    spec = importlib.util.spec_from_file_location("overlay", web / "apply_overlay.py")
    overlay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(overlay)
    form = tmp_path / "components/features/model-config-form.tsx"
    form.parent.mkdir(parents=True)
    original = source.read_text()
    form.write_text(original)
    overlay.apply(tmp_path)
    preserved = form.with_name("stock-model-config-form.tsx").read_text()
    assert preserved.count('window.dispatchEvent(new Event("sag:stock-model-saved"));') == 1
    restored = preserved.replace('\n      window.dispatchEvent(new Event("sag:stock-model-saved"));', "")
    restored = restored.replace('\nimport type { ModelSettingsProps } from "./model-config-form";', "")
    restored = restored.replace('export function ModelConfigForm({ saveRef, onState }: ModelSettingsProps = {}) {',
                                'export function ModelConfigForm() {')
    restored = restored.replace('\n\n  React.useImperativeHandle(saveRef, () => ({ save }));\n'
                                '  const changed = Object.keys(changedPatch()).length > 0;\n'
                                '  React.useEffect(() => {\n'
                                '    onState?.({ ready: Boolean(cfg && providers.length > 0 && !loadError && !saving && !testing), changed });\n'
                                '  }, [cfg, providers.length, loadError, saving, testing, changed, onState]);', '')
    start = restored.index('  function changedPatch(): ModelConfigPatch {')
    end = restored.index('  async function save() {', start)
    restored = restored[:start] + restored[end:]
    restored = restored.replace('  async function save() {\n    const patch = changedPatch();\n'
                                '    if (!Object.keys(patch).length) return;\n', '  async function save() {\n')
    restored = restored.replace('    try {\n\n      const { config } = await api.saveModelConfig(patch);',
                                '    try {\n      const patch = currentPatch();\n      const { config } = await api.saveModelConfig(patch);')
    restored = restored.replace('      hydrate(config);\n\n      getDiagnosticsStore().record("model.save", {',
                                '      hydrate(config);\n      await refreshCapabilities();\n      getDiagnosticsStore().record("model.save", {')
    restored = restored.replace('\n\n    } catch (error) {\n      throw new Error(error instanceof ApiError ? error.message : t("saveFailed"));',
                                '\n      toast.success(t("saved"));\n    } catch (error) {\n      toast.error(error instanceof ApiError ? error.message : t("saveFailed"));')
    restored = restored.replace("Check, Plug, RotateCw, Sparkles, X", "Check, Plug, RotateCw, Save, Sparkles, X")
    generation_marker = '      <SettingsSection title={t("generationTitle")} description={t("generationDescription")}>'
    generation_start = restored.index(generation_marker[:-1])
    generation_end = restored.index('      >\n        <SettingsRow', generation_start) + len('      >')
    generation_footer = restored[generation_start:generation_end]
    assert 'onClick={test}' in generation_footer and '{testResult.message}' in generation_footer
    assert preserved.count('onClick={test}') == 1 and preserved.count('{testResult.message}') == 1
    restored = restored[:generation_start] + generation_marker + restored[generation_end:]
    original_bottom = original.index('      <div className="flex flex-wrap items-center justify-between gap-3 border-t pt-4">')
    assert 'onClick={save}' not in preserved
    bottom_start = restored.rindex('    </div>\n  );\n}')
    restored = restored[:bottom_start] + original[original_bottom:]
    assert restored == original
    form.write_text(original + "\n// upstream update\n")
    with pytest.raises(SystemExit, match="Unsupported Model settings form"):
        overlay.apply(tmp_path)


@pytest.mark.parametrize("mode", ["disabled", "query"])
def test_fresh_bootstrap_lifespan(mode):
    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("smoke.py")), mode],
        env=os.environ.copy(), capture_output=True, text=True, timeout=45, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
