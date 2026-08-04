import { describe, expect, it } from "vitest";

import { isComposerCompositionKeyEvent } from "./composer-keyboard";

describe("chat composer IME handling", () => {
  it("does not submit Enter while an IME candidate is being composed", () => {
    expect(
      isComposerCompositionKeyEvent(
        { key: "Enter", nativeEvent: { isComposing: true } },
        { composing: false, commitGuard: false },
      ),
    ).toBe(true);
  });
});
