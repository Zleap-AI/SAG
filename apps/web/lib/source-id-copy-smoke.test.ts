import { describe, expect, it, vi } from "vitest";

import { runSourceIdCopy } from "@/components/features/source-id-copy";

describe("source ID copy entrypoint", () => {
  it("copies the complete source ID and reports success", async () => {
    const copy = vi.fn(async () => undefined);
    const onSuccess = vi.fn();
    const onFailure = vi.fn();

    await runSourceIdCopy({
      sourceId: "3fe9533639544615bc732d8d7a8f648e",
      copy,
      onSuccess,
      onFailure,
    });

    expect(copy).toHaveBeenCalledWith("3fe9533639544615bc732d8d7a8f648e");
    expect(onSuccess).toHaveBeenCalledOnce();
    expect(onFailure).not.toHaveBeenCalled();
  });
});
