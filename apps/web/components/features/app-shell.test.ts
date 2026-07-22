import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

const appShellSource = readFileSync(
  new URL("./app-shell.tsx", import.meta.url),
  "utf8",
);

describe("app shell viewport stage", () => {
  it("pins the universe stage to the visible viewport instead of content height", () => {
    const stageStart = appShellSource.indexOf("<DetailPanelProvider>");
    const backdropStart = appShellSource.indexOf(
      '<SpaceBackdrop variant={appMode === "explore" ? "universe" : "shell"} />',
      stageStart,
    );
    const stageOpening = appShellSource.slice(stageStart, backdropStart);

    expect(stageStart).toBeGreaterThanOrEqual(0);
    expect(backdropStart).toBeGreaterThan(stageStart);
    expect(stageOpening).toContain(
      '"bg-space-field relative grid h-svh min-h-0 overflow-hidden"',
    );
    expect(stageOpening).not.toContain(
      '"bg-space-field relative grid min-h-svh overflow-hidden"',
    );
  });

  it("keeps one backdrop mounted and selects the lightweight universe atmosphere in explore", () => {
    expect(appShellSource).toContain(
      '<SpaceBackdrop variant={appMode === "explore" ? "universe" : "shell"} />',
    );
    expect(appShellSource).not.toContain(
      '{appMode !== "explore" && <SpaceBackdrop />}',
    );
  });

  it("enters exploration from panels through a fresh universe home", () => {
    const start = appShellSource.indexOf(
      "const enterExploreMode = React.useCallback",
    );
    const end = appShellSource.indexOf(
      "const exitExploreMode = React.useCallback",
      start,
    );
    const enterExploreMode = appShellSource.slice(start, end);

    expect(start).toBeGreaterThanOrEqual(0);
    expect(end).toBeGreaterThan(start);
    expect(appShellSource).toContain("dispatchUniverseReset,");
    expect(enterExploreMode).toContain('if (appMode !== "explore")');
    expect(enterExploreMode).toContain('dispatchUniverseReset("explore-home")');
    expect(enterExploreMode.indexOf('dispatchUniverseReset("explore-home")'))
      .toBeLessThan(enterExploreMode.indexOf('setAppMode("explore")'));
  });
});
