import { describe, expect, it } from "vitest";

import { DiagnosticsStore, sanitize } from "./diagnostics";

describe("sanitize", () => {
  it("passes through primitives", () => {
    expect(sanitize(null)).toBeNull();
    expect(sanitize(undefined)).toBeUndefined();
    expect(sanitize(42)).toBe(42);
    expect(sanitize(true)).toBe(true);
    expect(sanitize("hello")).toBe("hello");
  });

  it("redacts api_key fields", () => {
    const input = {
      llm_api_key: "sk-secret-value-1234567890",
      name: "test",
    };
    const result = sanitize(input) as Record<string, unknown>;
    expect(result.llm_api_key).toBe("[REDACTED]");
    expect(result.name).toBe("test");
  });

  it("redacts fields with secret in the name", () => {
    const input = {
      client_secret: "abc123",
      SECRET_KEY: "xyz789",
    };
    const result = sanitize(input) as Record<string, unknown>;
    expect(result.client_secret).toBe("[REDACTED]");
    expect(result.SECRET_KEY).toBe("[REDACTED]");
  });

  it("redacts fields with token in the name", () => {
    const input = {
      access_token: "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
      refresh_token: "abcdef1234567890",
    };
    const result = sanitize(input) as Record<string, unknown>;
    expect(result.access_token).toBe("[REDACTED]");
    expect(result.refresh_token).toBe("[REDACTED]");
  });

  it("redacts fields with password in the name", () => {
    const input = {
      password: "my-secure-password",
      db_password: "admin123",
    };
    const result = sanitize(input) as Record<string, unknown>;
    expect(result.password).toBe("[REDACTED]");
    expect(result.db_password).toBe("[REDACTED]");
  });

  it("redacts fields with credential in the name", () => {
    const input = {
      credentials: "some-token-value",
    };
    const result = sanitize(input) as Record<string, unknown>;
    expect(result.credentials).toBe("[REDACTED]");
  });

  it("redacts keys named 'key' when the value looks like a secret", () => {
    const input = {
      key: "sk-abcdefghijklmnopqrstuvwxyz123456",
    };
    const result = sanitize(input) as Record<string, unknown>;
    expect(result.key).toBe("[REDACTED]");
  });

  it("does not redact short key values", () => {
    const input = {
      key: "short",
    };
    const result = sanitize(input) as Record<string, unknown>;
    expect(result.key).toBe("short");
  });

  it("recursively sanitizes nested objects", () => {
    const input = {
      llm: {
        api_key: "sk-nested-secret",
        model: "gpt-4",
        config: {
          secret_token: "nested-token",
          timeout: 30000,
        },
      },
    };
    const result = sanitize(input) as Record<string, unknown>;
    const llm = result.llm as Record<string, unknown>;
    expect(llm.api_key).toBe("[REDACTED]");
    expect(llm.model).toBe("gpt-4");
    const config = llm.config as Record<string, unknown>;
    expect(config.secret_token).toBe("[REDACTED]");
    expect(config.timeout).toBe(30000);
  });

  it("sanitizes arrays", () => {
    const input = [
      { api_key: "secret1", name: "a" },
      { api_key: "secret2", name: "b" },
    ];
    const result = sanitize(input) as Array<Record<string, unknown>>;
    expect(result[0].api_key).toBe("[REDACTED]");
    expect(result[0].name).toBe("a");
    expect(result[1].api_key).toBe("[REDACTED]");
    expect(result[1].name).toBe("b");
  });
});

describe("DiagnosticsStore", () => {
  it("records entries with auto-incrementing sequence", () => {
    const store = new DiagnosticsStore();
    store.record("app.init", { version: "1.0" });
    store.record("model.load", { provider: "openai" });

    const snapshot = store.snapshot();
    expect(snapshot).toHaveLength(2);
    expect(snapshot[0].seq).toBe(1);
    expect(snapshot[0].type).toBe("app.init");
    expect(snapshot[1].seq).toBe(2);
    expect(snapshot[1].type).toBe("model.load");
  });

  it("includes ISO 8601 timestamps", () => {
    const store = new DiagnosticsStore();
    store.record("error", { message: "test" });

    const snapshot = store.snapshot();
    expect(snapshot[0].ts).toMatch(
      /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/,
    );
  });

  it("enforces the buffer cap", () => {
    const store = new DiagnosticsStore(3);
    for (let i = 0; i < 5; i += 1) {
      store.record("app.init", { index: i });
    }
    const snapshot = store.snapshot();
    expect(snapshot).toHaveLength(3);
    expect(snapshot[0].data.index).toBe(2);
    expect(snapshot[2].data.index).toBe(4);
  });

  it("sanitizes data on record", () => {
    const store = new DiagnosticsStore();
    store.record("model.save", {
      llm_api_key: "sk-should-be-redacted",
      model: "gpt-4",
    });

    const snapshot = store.snapshot();
    expect(snapshot[0].data.llm_api_key).toBe("[REDACTED]");
    expect(snapshot[0].data.model).toBe("gpt-4");
  });

  it("exports with correct structure", () => {
    const store = new DiagnosticsStore();
    store.record("app.init", { language: "zh" });

    const export_ = store.export(
      { app: "web", user_agent: "test", language: "zh", timezone: "Asia/Shanghai" },
      { llm_provider: "openai", llm_model: "gpt-4", llm_api_key_set: true },
      { llm_configured: true },
    );

    expect(export_.version).toBe(1);
    expect(export_.exported_at).toMatch(/^\d{4}-\d{2}-\d{2}T/);
    expect(export_.environment.app).toBe("web");
    expect(export_.entries).toHaveLength(1);
    expect(export_.model_config).toEqual({
      llm_provider: "openai",
      llm_model: "gpt-4",
      llm_api_key_set: true,
    });
    expect(export_.capabilities).toEqual({ llm_configured: true });
  });

  it("sanitizes model config on export", () => {
    const store = new DiagnosticsStore();
    const export_ = store.export(
      { app: "web", user_agent: "test", language: "zh", timezone: "UTC" },
      { llm_api_key: "sk-secret", llm_model: "gpt-4" },
    );

    expect(export_.model_config?.llm_api_key).toBe("[REDACTED]");
    expect(export_.model_config?.llm_model).toBe("gpt-4");
  });

  it("returns count of entries", () => {
    const store = new DiagnosticsStore();
    expect(store.count).toBe(0);
    store.record("app.init");
    store.record("model.load");
    expect(store.count).toBe(2);
  });

  it("returns a defensive copy from snapshot", () => {
    const store = new DiagnosticsStore();
    store.record("app.init");
    const snapshot = store.snapshot();
    snapshot.push({ seq: 999, ts: "", type: "error", data: {} });
    expect(store.count).toBe(1);
  });

  it("returns the same snapshot reference when buffer unchanged", () => {
    const store = new DiagnosticsStore();
    store.record("app.init");
    const a = store.snapshot();
    const b = store.snapshot();
    expect(a).toBe(b);
  });

  it("invalidates snapshot cache on new record", () => {
    const store = new DiagnosticsStore();
    store.record("app.init");
    const a = store.snapshot();
    store.record("model.load");
    const b = store.snapshot();
    expect(a).not.toBe(b);
    expect(b).toHaveLength(2);
  });

  it("notifies subscribers on record", () => {
    const store = new DiagnosticsStore();
    let notified = false;
    store.subscribe(() => {
      notified = true;
    });
    store.record("app.init");
    expect(notified).toBe(true);
  });

  it("unsubscribe stops notifications", () => {
    const store = new DiagnosticsStore();
    let count = 0;
    const unsub = store.subscribe(() => {
      count += 1;
    });
    store.record("app.init");
    expect(count).toBe(1);
    unsub();
    store.record("model.load");
    expect(count).toBe(1);
  });
});
