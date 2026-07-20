"use client";

import * as React from "react";
import type { ISourceOptions } from "@tsparticles/engine";
import Particles, { ParticlesProvider } from "@tsparticles/react";
import { loadSlim } from "@tsparticles/slim";
import { useTheme } from "next-themes";

const BASE_PARTICLE_COUNT = 264;

function createParticleOptions(
  dark: boolean,
  reducedMotion: boolean,
  density = 1,
): ISourceOptions {
  return {
    // In graph detail mode this layer is visual context only. A static first
    // frame avoids running a second particle loop behind the WebGL scene.
    autoPlay: !reducedMotion,
    background: { color: { value: "transparent" } },
    detectRetina: true,
    fpsLimit: 24,
    fullScreen: { enable: false },
    pauseOnBlur: true,
    pauseOnOutsideViewport: true,
    particles: {
      color: {
        value: dark
          ? ["#ffffff", "#d7e1e8", "#d7e1e8", "#f2dfa2"]
          : ["#69727d", "#858e98", "#a0a5aa", "#b4965f"],
      },
      move: {
        direction: "none",
        enable: !reducedMotion,
        outModes: { default: "out" },
        random: true,
        speed: { min: 0.012, max: 0.045 },
        straight: false,
      },
      number: {
        density: { enable: true, height: 800, width: 1200 },
        value: Math.round(BASE_PARTICLE_COUNT * density),
      },
      opacity: {
        animation: {
          destroy: "none",
          enable: false,
          speed: 0,
          startValue: "random",
          sync: false,
        },
        value: dark ? { min: 0.18, max: 0.82 } : { min: 0.14, max: 0.48 },
      },
      shape: { type: "circle" },
      size: {
        animation: {
          destroy: "none",
          enable: false,
          speed: 0,
          startValue: "random",
          sync: false,
        },
        value: dark ? { min: 0.35, max: 1.45 } : { min: 0.35, max: 1.25 },
      },
    },
  };
}

export function SpaceParticles({
  reducedMotion = false,
  density = 1,
}: {
  reducedMotion?: boolean;
  /** Multiplies the sparse shell field for the immersive universe backdrop. */
  density?: number;
}) {
  const id = React.useId().replace(/:/g, "");
  const { resolvedTheme } = useTheme();
  const dark = resolvedTheme === "dark";
  const options = React.useMemo(
    () => createParticleOptions(dark, reducedMotion, density),
    [dark, density, reducedMotion],
  );
  const mountedRef = React.useRef(false);

  React.useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const handleParticlesLoaded = React.useCallback<
    NonNullable<React.ComponentProps<typeof Particles>["particlesLoaded"]>
  >((container) => {
    // If initialization finishes after unmount, tsparticles otherwise appends
    // its fallback canvas to document.body and keeps an orphan renderer alive.
    if (!mountedRef.current) container?.destroy();
    // `autoPlay: false` intentionally avoids a second animation loop behind
    // the WebGL scene, but it also skips tsParticles' first draw. Paint one
    // deterministic frame so the static deep-space star field remains
    // visible, then leave the container paused.
    if (mountedRef.current && reducedMotion && container) {
      container.canvas.render.drawParticles({ factor: 0, value: 0 });
    }
  }, [reducedMotion]);

  return (
    <ParticlesProvider init={loadSlim}>
      <Particles
        key={dark ? "dark" : "light"}
        id={`sag-space-${id}`}
        className="sag-space-particles"
        options={options}
        particlesLoaded={handleParticlesLoaded}
      />
    </ParticlesProvider>
  );
}
