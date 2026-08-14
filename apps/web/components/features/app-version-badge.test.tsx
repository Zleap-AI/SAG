import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import packageMetadata from "@/package.json";
import { AppVersionBadge } from "@/components/features/app-version-badge";

describe("AppVersionBadge", () => {
  it("shows the build version with a v prefix", () => {
    const html = renderToStaticMarkup(<AppVersionBadge />);

    expect(html).toContain(`v${packageMetadata.version}`);
  });
});
