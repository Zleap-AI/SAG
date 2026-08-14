import { afterEach, describe, expect, it, vi } from "vitest";

import { api, ApiError } from "./api";

class FakeXMLHttpRequest {
  static instances: FakeXMLHttpRequest[] = [];
  static nextResponse = {
    status: 201,
    body: '{"id":"doc-1","status":"pending"}',
    headers: { "X-Request-Id": "request-success" },
  };

  status = 0;
  responseText = "";
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  upload = {} as XMLHttpRequestUpload;
  readonly requestHeaders = new Map<string, string>();
  private readonly responseHeaders: Record<string, string>;

  constructor() {
    FakeXMLHttpRequest.instances.push(this);
    this.responseHeaders = FakeXMLHttpRequest.nextResponse.headers;
  }

  open() {}

  setRequestHeader(name: string, value: string) {
    this.requestHeaders.set(name, value);
  }

  getResponseHeader(name: string) {
    return this.responseHeaders[name] ?? null;
  }

  send() {
    this.status = FakeXMLHttpRequest.nextResponse.status;
    this.responseText = FakeXMLHttpRequest.nextResponse.body;
    queueMicrotask(() => this.onload?.());
  }
}

function setResponse(response: typeof FakeXMLHttpRequest.nextResponse) {
  FakeXMLHttpRequest.nextResponse = response;
}

afterEach(() => {
  FakeXMLHttpRequest.instances = [];
  setResponse({
    status: 201,
    body: '{"id":"doc-1","status":"pending"}',
    headers: { "X-Request-Id": "request-success" },
  });
  vi.unstubAllGlobals();
});

describe("uploadDocumentWithProgress", () => {
  it("returns the document and request ID while sending the supplied folder import batch ID", async () => {
    vi.stubGlobal("XMLHttpRequest", FakeXMLHttpRequest);

    const result = await api.uploadDocumentWithProgress(
      "source-1",
      new File(["# note"], "note.md", { type: "text/markdown" }),
      () => {},
      undefined,
      { folderImportId: "018f5f7e-89ab-7def-8123-0123456789a0" },
    );

    expect(result).toMatchObject({
      document: { id: "doc-1", status: "pending" },
      requestId: "request-success",
    });
    expect(FakeXMLHttpRequest.instances).toHaveLength(1);
    expect(FakeXMLHttpRequest.instances[0].requestHeaders.get("X-SAG-Folder-Import-Id")).toBe(
      "018f5f7e-89ab-7def-8123-0123456789a0",
    );
  });

  it("omits the folder import header when no batch is supplied", async () => {
    vi.stubGlobal("XMLHttpRequest", FakeXMLHttpRequest);

    await api.uploadDocumentWithProgress(
      "source-1",
      new File(["# note"], "note.md", { type: "text/markdown" }),
      () => {},
    );

    expect(FakeXMLHttpRequest.instances[0].requestHeaders.has("X-SAG-Folder-Import-Id")).toBe(false);
  });

  it("exposes a failed response envelope request ID on ApiError", async () => {
    setResponse({
      status: 422,
      body: '{"error":{"message":"文件夹导入批次标识无效","request_id":"request-rejected"}}',
      headers: { "X-Request-Id": "request-header" },
    });
    vi.stubGlobal("XMLHttpRequest", FakeXMLHttpRequest);

    await expect(
      api.uploadDocumentWithProgress(
        "source-1",
        new File(["# note"], "note.md", { type: "text/markdown" }),
        () => {},
      ),
    ).rejects.toMatchObject({
      status: 422,
      code: "upload_failed",
      requestId: "request-rejected",
    } satisfies Partial<ApiError>);
  });
});
