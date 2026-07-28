import { fileURLToPath, URL } from "node:url";

import ts from "typescript";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [
    {
      name: "transform-test-tsx",
      enforce: "pre",
      transform(code, id) {
        if (!id.split("?")[0].endsWith(".tsx")) return;
        const result = ts.transpileModule(code, {
          fileName: id,
          compilerOptions: {
            jsx: ts.JsxEmit.ReactJSX,
            module: ts.ModuleKind.ESNext,
            target: ts.ScriptTarget.ES2021,
          },
        });
        return { code: result.outputText, map: null };
      },
    },
  ],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL(".", import.meta.url)),
    },
  },
});
