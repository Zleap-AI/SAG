import { describe, expect, it } from "vitest";

import {
  classifyUniverseWebGLContextFailure,
  detectUniverseWebGLCapability,
  isUniverseWebGLContextCreationError,
} from "./universe-webgl-capability";

describe("universe WebGL capability", () => {
  it("detects the WebGL2 API without allocating a probe context", () => {
    class WebGL2RenderingContext {}

    expect(detectUniverseWebGLCapability({ WebGL2RenderingContext })).toBe("available");
    expect(detectUniverseWebGLCapability({})).toBe("api-unavailable");
    expect(detectUniverseWebGLCapability(undefined)).toBe("api-unavailable");
  });

  it("classifies Three context allocation errors separately from scene errors", () => {
    expect(isUniverseWebGLContextCreationError(
      new Error("THREE.WebGLRenderer: Error creating WebGL context."),
    )).toBe(true);
    expect(isUniverseWebGLContextCreationError(
      new Error("WebGL context could not be allocated by the GPU process"),
    )).toBe(true);
    expect(isUniverseWebGLContextCreationError(
      new Error("Failed to build deterministic graph positions"),
    )).toBe(false);
  });

  it("identifies a browser-disabled graphics stack separately from pressure", () => {
    const disabled = new Error(
      "THREE.WebGLRenderer: A WebGL context could not be created. "
      + "GL_VENDOR = Disabled, GL_RENDERER = Disabled",
    );
    expect(classifyUniverseWebGLContextFailure(disabled)).toBe("context-disabled");
    expect(classifyUniverseWebGLContextFailure(
      new Error("THREE.WebGLRenderer: Error creating WebGL context."),
    )).toBe("context-creation");
    expect(classifyUniverseWebGLContextFailure(
      new Error("Failed to build deterministic graph positions"),
    )).toBeNull();
  });

  it("follows a bounded error cause chain", () => {
    const root = new Error("THREE.WebGLRenderer: Error creating WebGL context");
    expect(isUniverseWebGLContextCreationError(new Error("scene startup", { cause: root })))
      .toBe(true);
  });
});
