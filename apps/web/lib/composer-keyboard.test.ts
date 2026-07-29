import { describe, expect, it } from "vitest";

import {
  isComposerCompositionKeyEvent,
  shouldSubmitAfterEnter,
} from "./composer-keyboard";

describe("isComposerCompositionKeyEvent", () => {
  it("blocks Enter while a composition session is active", () => {
    expect(
      isComposerCompositionKeyEvent(keyEvent("Enter"), {
        composing: true,
        commitGuard: false,
      }),
    ).toBe(true);
  });

  it("blocks Enter reported as a native composition event", () => {
    expect(
      isComposerCompositionKeyEvent(keyEvent("Enter", { isComposing: true }), {
        composing: false,
        commitGuard: false,
      }),
    ).toBe(true);
  });

  it("blocks legacy IME keyCode 229 events", () => {
    expect(
      isComposerCompositionKeyEvent(keyEvent("Enter", { keyCode: 229 }), {
        composing: false,
        commitGuard: false,
      }),
    ).toBe(true);
  });

  it("blocks only Enter during the post-composition guard", () => {
    expect(
      isComposerCompositionKeyEvent(keyEvent("Enter"), {
        composing: false,
        commitGuard: true,
      }),
    ).toBe(true);
    expect(
      isComposerCompositionKeyEvent(keyEvent("a"), {
        composing: false,
        commitGuard: true,
      }),
    ).toBe(false);
  });

  it("allows a normal Enter key press", () => {
    expect(
      isComposerCompositionKeyEvent(keyEvent("Enter"), {
        composing: false,
        commitGuard: false,
      }),
    ).toBe(false);
  });
});

describe("shouldSubmitAfterEnter", () => {
  it("does not submit when Enter commits an IME candidate", () => {
    expect(shouldSubmitAfterEnter("ni'hao", "你好")).toBe(false);
  });

  it("submits when Enter only adds the textarea newline", () => {
    expect(shouldSubmitAfterEnter("hello", "hello\n")).toBe(true);
  });
});

function keyEvent(
  key: string,
  nativeEvent: { isComposing?: boolean; keyCode?: number } = {},
) {
  return { key, nativeEvent };
}
