import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

const source = readFileSync(
  new URL("./space-backdrop.tsx", import.meta.url),
  "utf8",
);
const particleSource = readFileSync(
  new URL("./space-particles.tsx", import.meta.url),
  "utf8",
);
const galaxySource = readFileSync(
  new URL("./particle-galaxy.tsx", import.meta.url),
  "utf8",
);

function sourceBetween(start: string, end: string) {
  const startIndex = source.indexOf(start);
  const endIndex = source.indexOf(end, startIndex + start.length);
  expect(startIndex).toBeGreaterThanOrEqual(0);
  expect(endIndex).toBeGreaterThan(startIndex);
  return source.slice(startIndex, endIndex);
}

describe("space backdrop interaction isolation", () => {
  it("reuses one shared atmosphere across the shell and universe", () => {
    expect(source).toContain('variant?: "shell" | "universe"');
    expect(source).toContain('data-space-variant={variant}');
    expect(source).toContain('<span className="sag-space-galaxy-orbit">');
    expect(source).toContain('<span className="sag-space-dust" />');
    expect(source).toContain("reducedMotion={ambientMotionPaused}");
    expect(source).toContain("pauseAmbientMotion = false");
  });

  it("uses universe view only as an overview/detail visibility gate", () => {
    const viewEffect = sourceBetween(
      "React.useEffect(() => {\n    const backdrop = backdropRef.current;",
      "React.useEffect(() => {\n    if (reducedMotion || !cursorMeteorRef.current) return;",
    );

    expect(viewEffect).toContain("readUniverseView()");
    expect(viewEffect).toContain("UNIVERSE_VIEW_EVENT");
    expect(viewEffect).toContain(
      'view.mode === "detail" || view.progress >= 0.12',
    );
    expect(viewEffect).toContain('backdrop.dataset.universeView = nextView');
    expect(viewEffect).toContain(
      'backdrop.dataset.ambientMotion = backdropMotionPaused || detail ? "paused" : "active"',
    );
    expect(viewEffect).toContain('cursorMeteorRef.current.dataset.active = "false"');
    expect(viewEffect).not.toMatch(/style\.|transform|translate|rotate|scale/);
    expect(source).not.toContain("useTransform");
  });

  it("keeps accumulated evidence static with scene-owned universe decoration", () => {
    expect(source).toContain("UNIVERSE_PRESENTATION_EVENT");
    expect(source).toContain(
      'universePresentation === "accumulation"',
    );
    expect(source).toContain('const universeBackdrop = variant === "universe"');
    expect(source).toContain(
      "{!universeBackdrop && !accumulationPresentation && (",
    );
    expect(source).toContain(
      "reducedMotion={universeBackdrop || backdropMotionPaused}",
    );
    expect(source).toContain(
      'data-universe-presentation={variant === "universe" ? universePresentation : "fixed"}',
    );
  });

  it("resets the visual atmosphere when explore is entered again", () => {
    expect(source).toContain("const universeVariantRef = React.useRef(false)");
    expect(source).toContain("const enteringUniverse = !universeVariantRef.current");
    expect(source).toContain('backdrop.dataset.universeView = "overview"');
    expect(source).toContain('if (!enteringUniverse) syncView(readUniverseView())');
  });

  it("keeps the universe sky quiet while the shell retains its rich atmosphere", () => {
    expect(source).toContain("density={universeBackdrop ? 0.52 : 1}");
    expect(source).toContain("{!universeBackdrop && SPARKLES.map");
    expect(particleSource).toContain("const BASE_PARTICLE_COUNT = 264");
    expect(galaxySource).toContain("const PARTICLE_ALPHA_SCALE = 1.65");
    expect(galaxySource).toContain("createHaloParticles(random, 520)");
    expect(galaxySource).toContain("createArmParticles(random, 4480)");
    expect(galaxySource).toContain("createCoreParticles(random, 640)");
    const css = readFileSync(
      new URL("../../app/globals.css", import.meta.url),
      "utf8",
    );
    expect(css).toContain(
      '.sag-space-sparkles[data-space-variant="universe"]::before {\n    content: none;',
    );
    expect(css).toContain(
      '.sag-space-sparkles[data-space-variant="universe"] .sag-space-particles {\n    animation: none;',
    );
    expect(css).toContain("sag-universe-overview-breathe");
    expect(css).toContain(
      '.sag-knowledge-universe[data-universe-mode="explore"][data-universe-view="overview"]',
    );
    expect(css).toContain("hsl(45 100% 88% / 0.92)");
  });

  it("gates the cursor meteor for drags and source-detail exploration", () => {
    expect(source).toContain("event.buttons !== 0");
    expect(source).toContain('window.addEventListener("pointermove", handlePointerMove,');
    expect(source).toContain("capture: true");
    expect(source).toContain("sag-space-cursor-layer");
    expect(source).toContain(
      'variant === "universe" && universeDetailRef.current',
    );
    expect(source.indexOf("event.buttons !== 0"))
      .toBeLessThan(source.indexOf("pendingFrame = { x, y, speed, angle }"));
  });

  it("seeds its first point, ignores low-speed jitter and normalizes pointer polling rate", () => {
    const pointerMove = sourceBetween(
      "const handlePointerMove = (event: PointerEvent) =>",
      "const resizeObserver = new ResizeObserver(measureField)",
    );

    expect(pointerMove).toContain("if (!hasPreviousPoint)");
    expect(pointerMove).toContain("previousTime = event.timeStamp");
    expect(pointerMove).toContain("hasPreviousPoint = true;\n        return;");
    expect(pointerMove).toContain(
      "Math.max(8, Math.min(50, event.timeStamp - previousTime || 16.67))",
    );
    expect(pointerMove).toContain("* (16.67 / elapsed)");
    expect(pointerMove).toContain("if (speed <= 0.7) return");
    expect(pointerMove.indexOf("if (speed <= 0.7) return"))
      .toBeLessThan(pointerMove.indexOf("pendingFrame = { x, y, speed, angle }"));
  });

  it("coalesces cursor meteor work to one frame and cleans it up without pointer-time layout reads", () => {
    const pointerMove = sourceBetween(
      "const handlePointerMove = (event: PointerEvent) =>",
      "const resizeObserver = new ResizeObserver(measureField)",
    );
    const cleanup = sourceBetween(
      "return () => {\n      resizeObserver.disconnect();",
      "}, [cursorPortalRoot, reducedMotion, variant]);",
    );

    expect(source).toContain("let fieldBounds = field.getBoundingClientRect()");
    expect(pointerMove).toContain("fieldBounds.left");
    expect(pointerMove).toContain("window.requestAnimationFrame(renderMeteor)");
    expect(pointerMove).not.toContain("getBoundingClientRect()");
    expect(cleanup).toContain("window.cancelAnimationFrame(animationFrame)");
    expect(cleanup).toContain("window.clearTimeout(hideTimer)");
  });
});
