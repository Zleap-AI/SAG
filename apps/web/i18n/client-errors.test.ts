import { describe, expect, it } from "vitest";

import { serverErrorMessage } from "./client-errors";

describe("serverErrorMessage", () => {
  it("preserves server details in the Chinese interface", () => {
    expect(serverErrorMessage("not_found", "文档不存在", 404, "zh-CN"))
      .toBe("文档不存在");
  });

  it("preserves server details that already match the English interface", () => {
    expect(serverErrorMessage("not_found", "Document not found", 404, "en-US"))
      .toBe("Document not found");
  });

  it("uses the error code to replace untranslated implementation text", () => {
    expect(serverErrorMessage("configuration_error", "尚未配置 LLM", 400, "en-US"))
      .toBe("The service is not fully configured. Complete the required settings first.");
  });

  it("falls back to the HTTP status when the code is not specific", () => {
    expect(serverErrorMessage("error", "信息源不存在", 404, "en-US"))
      .toBe("The requested resource was not found");
  });
});
