"use client";

import * as React from "react";
import dynamic from "next/dynamic";
import { useReducedMotion } from "motion/react";
import { createPortal } from "react-dom";
import { ParticleGalaxy } from "@/components/features/particle-galaxy";
import {
  readUniversePresentation,
  readUniverseView,
  UNIVERSE_PRESENTATION_EVENT,
  UNIVERSE_VIEW_EVENT,
  type UniversePresentationMode,
  type UniverseViewState,
} from "@/lib/universe-events";

const SpaceParticles = dynamic(
  () => import("@/components/features/space-particles").then((module) => module.SpaceParticles),
  { ssr: false },
);

const SPARKLES = [
  { x: 7, y: 18, size: 7, delay: -1.2, duration: 8.8 },
  { x: 12, y: 49, size: 4, delay: -9.4, duration: 12.4 },
  { x: 18, y: 72, size: 5, delay: -5.8, duration: 11.4 },
  { x: 24, y: 8, size: 6, delay: -3.1, duration: 10.2 },
  { x: 29, y: 34, size: 4, delay: -8.1, duration: 13.2 },
  { x: 33, y: 63, size: 5, delay: -11.2, duration: 14.6 },
  { x: 38, y: 84, size: 6, delay: -3.4, duration: 10.6 },
  { x: 46, y: 43, size: 4, delay: -6.3, duration: 11.9 },
  { x: 51, y: 12, size: 5, delay: -7.6, duration: 12.8 },
  { x: 57, y: 89, size: 5, delay: -1.8, duration: 9.8 },
  { x: 62, y: 58, size: 7, delay: -2.7, duration: 9.6 },
  { x: 68, y: 8, size: 6, delay: -12.6, duration: 15.1 },
  { x: 73, y: 27, size: 4, delay: -10.2, duration: 14.2 },
  { x: 77, y: 56, size: 5, delay: -6.9, duration: 12.1 },
  { x: 82, y: 78, size: 5, delay: -4.9, duration: 11.8 },
  { x: 88, y: 15, size: 4, delay: -2.4, duration: 9.4 },
  { x: 93, y: 44, size: 6, delay: -8.8, duration: 13.6 },
  { x: 96, y: 88, size: 6, delay: -10.7, duration: 14.8 },
  { x: 4, y: 36, size: 4, delay: -4.2, duration: 12.6 },
  { x: 15, y: 11, size: 3, delay: -12.4, duration: 15.8 },
  { x: 21, y: 91, size: 5, delay: -7.1, duration: 16.4 },
  { x: 35, y: 18, size: 4, delay: -1.6, duration: 11.2 },
  { x: 42, y: 71, size: 3, delay: -9.9, duration: 14.2 },
  { x: 55, y: 34, size: 4, delay: -5.2, duration: 12.9 },
  { x: 66, y: 76, size: 5, delay: -13.1, duration: 17.2 },
  { x: 71, y: 93, size: 3, delay: -3.8, duration: 10.8 },
  { x: 85, y: 38, size: 4, delay: -11.6, duration: 15.4 },
  { x: 98, y: 63, size: 5, delay: -6.4, duration: 13.2 },
] as const;

export function SpaceBackdrop({
  pauseAmbientMotion = false,
  variant = "shell",
}: {
  pauseAmbientMotion?: boolean;
  variant?: "shell" | "universe";
}) {
  const reducedMotion = useReducedMotion();
  const ambientMotionPaused = Boolean(reducedMotion) || pauseAmbientMotion;
  const backdropRef = React.useRef<HTMLDivElement>(null);
  const cursorMeteorRef = React.useRef<HTMLSpanElement>(null);
  const cursorPortalRootRef = React.useRef<HTMLDivElement | null>(null);
  const universeDetailRef = React.useRef(false);
  const universeVariantRef = React.useRef(false);
  const [universePresentation, setUniversePresentation] =
    React.useState<UniversePresentationMode>(readUniversePresentation);
  const [cursorPortalRoot, setCursorPortalRoot] = React.useState<HTMLDivElement | null>(null);
  const accumulationPresentation = variant === "universe"
    && universePresentation === "accumulation";
  const backdropMotionPaused = ambientMotionPaused || accumulationPresentation;
  const universeBackdrop = variant === "universe";

  React.useEffect(() => {
    if (variant !== "universe") return;
    const syncPresentation = (event: Event) => {
      setUniversePresentation(
        (event as CustomEvent<UniversePresentationMode>).detail,
      );
    };
    setUniversePresentation(readUniversePresentation());
    window.addEventListener(UNIVERSE_PRESENTATION_EVENT, syncPresentation);
    return () => window.removeEventListener(
      UNIVERSE_PRESENTATION_EVENT,
      syncPresentation,
    );
  }, [variant]);

  React.useEffect(() => {
    if (variant !== "universe" || reducedMotion) {
      cursorPortalRootRef.current = null;
      setCursorPortalRoot(null);
      return;
    }

    // The graph owns the full viewport and intentionally stops pointer
    // propagation on labels. Keep the cursor trail in a top-level overlay so
    // it remains visible above the transparent WebGL canvas and still receives
    // capture-phase pointer movement over every surface.
    const root = document.createElement("div");
    root.className = "sag-space-cursor-layer";
    root.dataset.universeView = "overview";
    root.setAttribute("aria-hidden", "true");
    document.body.appendChild(root);
    cursorPortalRootRef.current = root;
    setCursorPortalRoot(root);

    return () => {
      if (cursorPortalRootRef.current === root) cursorPortalRootRef.current = null;
      setCursorPortalRoot((current) => (current === root ? null : current));
      root.remove();
    };
  }, [reducedMotion, variant]);

  React.useEffect(() => {
    const backdrop = backdropRef.current;
    if (!backdrop) return;

    if (variant !== "universe") {
      universeVariantRef.current = false;
      universeDetailRef.current = false;
      backdrop.dataset.universeView = "fixed";
      backdrop.dataset.ambientMotion = backdropMotionPaused ? "paused" : "active";
      return;
    }

    // AppShell keeps the scene mounted while switching normal/explore modes.
    // A source-detail view can therefore be the last global view we saw when
    // the user re-enters explore. Treat every new universe mount as its clean
    // overview entrance; the scene will publish detail again when it actually
    // flies into a source.
    const enteringUniverse = !universeVariantRef.current;
    universeVariantRef.current = true;
    if (enteringUniverse) {
      universeDetailRef.current = false;
      backdrop.dataset.universeView = "overview";
      backdrop.dataset.ambientMotion = backdropMotionPaused ? "paused" : "active";
    }

    const syncView = (view: UniverseViewState) => {
      // The threshold is intentionally crossed once during the source dive.
      // CSS owns the fade; React never re-renders on camera progress frames.
      const detail = view.mode === "detail" || view.progress >= 0.12;
      const nextView = detail ? "detail" : "overview";
      if (
        detail !== universeDetailRef.current
        || backdrop.dataset.universeView !== nextView
      ) {
        universeDetailRef.current = detail;
        backdrop.dataset.universeView = nextView;
      }
      if (cursorPortalRootRef.current) {
        cursorPortalRootRef.current.dataset.universeView = nextView;
      }
      backdrop.dataset.ambientMotion = backdropMotionPaused || detail ? "paused" : "active";
      if (detail && cursorMeteorRef.current) {
        cursorMeteorRef.current.dataset.active = "false";
      }
    };
    const handleView = (event: Event) => {
      syncView((event as CustomEvent<UniverseViewState>).detail);
    };

    if (!enteringUniverse) syncView(readUniverseView());
    window.addEventListener(UNIVERSE_VIEW_EVENT, handleView);
    return () => window.removeEventListener(UNIVERSE_VIEW_EVENT, handleView);
  }, [backdropMotionPaused, variant]);

  React.useEffect(() => {
    if (reducedMotion || !cursorMeteorRef.current) return;

    const meteor = cursorMeteorRef.current;
    const field = backdropRef.current?.closest<HTMLElement>(".bg-space-field");
    if (!field) return;

    let hideTimer: number | undefined;
    let animationFrame: number | undefined;
    let hasPreviousPoint = false;
    let previousX = 0;
    let previousY = 0;
    let previousTime = 0;
    let angle = -0.35;
    let fieldBounds = field.getBoundingClientRect();
    let pendingFrame: {
      x: number;
      y: number;
      speed: number;
      angle: number;
    } | null = null;

    const measureField = () => {
      fieldBounds = field.getBoundingClientRect();
    };

    const hideMeteor = () => {
      if (meteor.dataset.active !== "false") meteor.dataset.active = "false";
      hasPreviousPoint = false;
      pendingFrame = null;
      if (animationFrame !== undefined) {
        window.cancelAnimationFrame(animationFrame);
        animationFrame = undefined;
      }
      if (hideTimer) window.clearTimeout(hideTimer);
    };

    const renderMeteor = () => {
      animationFrame = undefined;
      const frame = pendingFrame;
      pendingFrame = null;
      if (!frame) return;
      meteor.style.transform = `translate3d(${frame.x}px, ${frame.y}px, 0) rotate(${frame.angle}rad)`;
      meteor.style.setProperty(
        "--cursor-meteor-tail",
        `${Math.min(138, 46 + frame.speed * 3.4)}px`,
      );
      if (meteor.dataset.active !== "true") meteor.dataset.active = "true";
      if (hideTimer) window.clearTimeout(hideTimer);
      hideTimer = window.setTimeout(hideMeteor, 210);
    };

    const handlePointerMove = (event: PointerEvent) => {
      const isInsideField =
        event.clientX >= fieldBounds.left
        && event.clientX <= fieldBounds.right
        && event.clientY >= fieldBounds.top
        && event.clientY <= fieldBounds.bottom;

      if (
        event.pointerType !== "mouse"
        || event.buttons !== 0
        || !isInsideField
        || (variant === "universe" && universeDetailRef.current)
      ) {
        hideMeteor();
        return;
      }

      const x = cursorPortalRoot ? event.clientX : event.clientX - fieldBounds.left;
      const y = cursorPortalRoot ? event.clientY : event.clientY - fieldBounds.top;
      if (!hasPreviousPoint) {
        previousX = x;
        previousY = y;
        previousTime = event.timeStamp;
        hasPreviousPoint = true;
        return;
      }
      const deltaX = x - previousX;
      const deltaY = y - previousY;
      const elapsed = Math.max(8, Math.min(50, event.timeStamp - previousTime || 16.67));
      // Normalize to a 60 Hz frame so a high-polling mouse does not produce a
      // shorter trail than the same physical gesture on a standard display.
      const speed = Math.hypot(deltaX, deltaY) * (16.67 / elapsed);

      if (speed > 0.7) {
        const nextAngle = Math.atan2(deltaY, deltaX);
        const angleDelta = Math.atan2(
          Math.sin(nextAngle - angle),
          Math.cos(nextAngle - angle),
        );
        angle += angleDelta * 0.42;
      }

      previousX = x;
      previousY = y;
      previousTime = event.timeStamp;

      if (speed <= 0.7) return;

      pendingFrame = { x, y, speed, angle };
      if (animationFrame === undefined) {
        animationFrame = window.requestAnimationFrame(renderMeteor);
      }
    };

    const resizeObserver = new ResizeObserver(measureField);
    resizeObserver.observe(field);
    window.addEventListener("resize", measureField, { passive: true });
    // Capture before graph labels stop propagation. The loading shell and the
    // explore graph then share one reliable cursor surface.
    window.addEventListener("pointermove", handlePointerMove, {
      capture: true,
      passive: true,
    });
    field.addEventListener("pointerleave", hideMeteor);
    window.addEventListener("blur", hideMeteor);
    document.addEventListener("visibilitychange", hideMeteor);

    return () => {
      resizeObserver.disconnect();
      window.removeEventListener("resize", measureField);
      window.removeEventListener("pointermove", handlePointerMove, true);
      field.removeEventListener("pointerleave", hideMeteor);
      window.removeEventListener("blur", hideMeteor);
      document.removeEventListener("visibilitychange", hideMeteor);
      if (animationFrame !== undefined) window.cancelAnimationFrame(animationFrame);
      if (hideTimer) window.clearTimeout(hideTimer);
    };
  }, [cursorPortalRoot, reducedMotion, variant]);

  return (
    <div
      ref={backdropRef}
      className="sag-space-sparkles"
      data-space-variant={variant}
      data-universe-view={variant === "universe" ? "overview" : "fixed"}
      data-universe-presentation={variant === "universe" ? universePresentation : "fixed"}
      data-ambient-motion={backdropMotionPaused ? "paused" : "active"}
      aria-hidden
    >
      <SpaceParticles
        // In the universe the shared field is only a quiet, distant sky. The
        // scene engine owns the sole moving/data-bearing nebula, so background
        // particles never compete with source dust or timeline particles.
        reducedMotion={universeBackdrop || backdropMotionPaused}
        density={universeBackdrop ? 0.52 : 1}
      />
      {!reducedMotion && (cursorPortalRoot
        ? createPortal(
          <span ref={cursorMeteorRef} className="sag-space-cursor-meteor" data-active="false" />,
          cursorPortalRoot,
        )
        : <span ref={cursorMeteorRef} className="sag-space-cursor-meteor" data-active="false" />)}
      {!universeBackdrop && !accumulationPresentation && (
        <>
          <span className="sag-space-galaxy-orbit">
            <ParticleGalaxy reducedMotion={ambientMotionPaused} />
          </span>
          <span className="sag-space-dust" />
          <span className="sag-space-meteor sag-space-meteor--one" />
          <span className="sag-space-meteor sag-space-meteor--two" />
          <span className="sag-space-meteor sag-space-meteor--three" />
          <span className="sag-space-meteor sag-space-meteor--four" />
        </>
      )}
      {!universeBackdrop && SPARKLES.map((star) => (
        <span
          key={`${star.x}-${star.y}`}
          className="sag-space-sparkle"
          style={
            {
              "--star-x": `${star.x}%`,
              "--star-y": `${star.y}%`,
              "--star-size": `${star.size}px`,
              "--star-delay": `${star.delay}s`,
              "--star-duration": `${star.duration}s`,
            } as React.CSSProperties
          }
        />
      ))}
    </div>
  );
}
