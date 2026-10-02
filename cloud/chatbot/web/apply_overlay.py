"""Apply an audited overlay to a disposable stock web build directory only."""
import hashlib
import json
import shutil
import sys
import textwrap
from pathlib import Path


def apply(destination):
    extension = Path(__file__).resolve().parent
    contract = json.loads((extension / "compatibility.json").read_text())
    form = destination / "components/features/model-config-form.tsx"
    if hashlib.sha256(form.read_bytes()).hexdigest() != contract["components/features/model-config-form.tsx"]:
        raise SystemExit("Unsupported Model settings form; review cloud/chatbot web overlay and build tests before upgrading")
    # Keep stock controls/save flows; place the generation test in its section.
    source = form.read_text()
    marker = "await refreshCapabilities();"
    if source.count(marker) != 2:
        raise SystemExit("Stock settings save flow changed; review the chatbot overlay")
    # The shared Save refreshes after either write; keep the stock quick setup notification.
    source = source.replace(marker, marker + '\n      window.dispatchEvent(new Event("sag:stock-model-saved"));')
    import_marker = 'import * as React from "react";'
    if source.count(import_marker) != 1:
        raise SystemExit("Stock model form imports changed; review the chatbot overlay")
    source = source.replace(import_marker, import_marker + '\nimport type { ModelSettingsProps } from "./model-config-form";')
    generation_marker = '      <SettingsSection title={t("generationTitle")} description={t("generationDescription")}>'
    result_marker = '        <div className="min-h-5 min-w-0">'
    actions_marker = '        <div className="flex flex-wrap items-center gap-2">'
    test_marker = '          <Button type="button" onClick={test} variant="outline" disabled={llmLocked || testing || saving}>'
    bottom_marker = '      <div className="flex flex-wrap items-center justify-between gap-3 border-t pt-4">'
    if any(source.count(marker) != 1 for marker in (generation_marker, result_marker, actions_marker, test_marker, bottom_marker)):
        raise SystemExit("Stock generation test controls changed; review the chatbot overlay")
    result = source[source.index(result_marker):source.index(actions_marker)]
    test_start = source.index(test_marker)
    test_end = source.index('          </Button>\n', test_start) + len('          </Button>\n')
    test = source[test_start:test_end]
    # Move the existing result and button verbatim, retaining their handlers/locks.
    source = source.replace(result, "").replace(test, "")
    generation_footer = ('\n        footer={\n          <div className="flex flex-wrap items-center justify-between gap-3">\n'
                         + textwrap.indent(textwrap.dedent(result), "            ")
                         + textwrap.indent(textwrap.dedent(test), "            ")
                         + '          </div>\n        }\n      >')
    source = source.replace(generation_marker, generation_marker[:-1] + generation_footer)
    # The page owns the only Save button. Expose the original handler through a ref.
    signature = "export function ModelConfigForm() {"
    load_hook = "  React.useEffect(() => {\n    void load();\n  }, [load]);"
    success = '      toast.success(t("saved"));'
    failure = '      toast.error(error instanceof ApiError ? error.message : t("saveFailed"));'
    save_start = "  async function save() {\n    setSaving(true);"
    save_patch = "      const patch = currentPatch();"
    if any(source.count(marker) != 1 for marker in (signature, load_hook, success, failure, save_start, save_patch)):
        raise SystemExit("Stock model save handler changed; review the chatbot overlay")
    source = source.replace(signature, "export function ModelConfigForm({ saveRef, onState }: ModelSettingsProps = {}) {")
    # Compare hydrated defaults/nulls and omit deployment-locked fields. Tests still use the full draft.
    changed_patch = textwrap.dedent('''\
      function changedPatch(): ModelConfigPatch {
        if (!cfg) return {};
        const configured: Record<string, unknown> = { ...cfg,
          llm_base_url: cfg.llm_base_url ?? null,
          llm_timeout_ms: cfg.llm_timeout_ms ?? 60_000,
          llm_max_retries: cfg.llm_max_retries ?? 2,
          llm_context_window: cfg.llm_context_window ?? 128000,
          embedding_base_url: cfg.embedding_base_url ?? "",
          embedding_dimensions: cfg.embedding_dimensions ?? null,
          mineru_base_url: cfg.mineru_base_url ?? null,
        };
        return Object.fromEntries(Object.entries(currentPatch()).filter(([key, value]) =>
          !cfg.locked_fields.includes(key) && value !== configured[key]));
      }

    ''')
    source = source.replace(save_start, textwrap.indent(changed_patch, "  ") +
                            "  async function save() {\n    const patch = changedPatch();\n"
                            "    if (!Object.keys(patch).length) return;\n    setSaving(true);")
    source = source.replace(save_patch, "")
    source = source.replace(load_hook, load_hook + '\n\n  React.useImperativeHandle(saveRef, () => ({ save }));\n'
                            '  const changed = Object.keys(changedPatch()).length > 0;\n'
                            '  React.useEffect(() => {\n'
                            '    onState?.({ ready: Boolean(cfg && providers.length > 0 && !loadError && !saving && !testing), changed });\n'
                            '  }, [cfg, providers.length, loadError, saving, testing, changed, onState]);')
    source = source.replace(success, "")
    source = source.replace(failure, '      throw new Error(error instanceof ApiError ? error.message : t("saveFailed"));')
    # The page refreshes after each independent save succeeds.
    source = source.replace('      await refreshCapabilities();\n      window.dispatchEvent(new Event("sag:stock-model-saved"));', "", 1)
    source = source[:source.index(bottom_marker)] + source[source.rindex('    </div>\n  );\n}'):]
    source = source.replace("Check, Plug, RotateCw, Save, Sparkles, X", "Check, Plug, RotateCw, Sparkles, X")
    form.with_name("stock-model-config-form.tsx").write_text(source)
    for name in ("model-config-form.tsx", "chatbot-config-sections.tsx", "chatbot-config-sections.test.tsx",
                 "model-config-form.test.tsx"):
        shutil.copyfile(extension / name, form.with_name(name))


if __name__ == "__main__":
    apply(Path(sys.argv[1]).resolve())
