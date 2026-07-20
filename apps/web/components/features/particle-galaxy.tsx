"use client";

import * as React from "react";
import { useTheme } from "next-themes";

const LOGICAL_WIDTH = 1000;
const LOGICAL_HEIGHT = 620;
const RENDER_SCALE = 1.5;
const CANVAS_WIDTH = LOGICAL_WIDTH * RENDER_SCALE;
const CANVAS_HEIGHT = LOGICAL_HEIGHT * RENDER_SCALE;
const CENTER_X = LOGICAL_WIDTH / 2;
const CENTER_Y = LOGICAL_HEIGHT / 2;
const MAX_RADIUS = 430;
const FRAME_INTERVAL = 1000 / 24;
const FULL_CIRCLE = Math.PI * 2;
const PARTICLE_ALPHA_SCALE = 1.65;

type Random = () => number;

type GalaxyParticle = {
  radius: number;
  angle: number;
  crossArm: number;
  size: number;
  alpha: number;
  hue: number;
  saturation: number;
  lightness: number;
  phase: number;
  twinkle: number;
  spin: number;
  drift: number;
};

function seededRandom(seed: number): Random {
  let state = seed >>> 0;
  return () => {
    state += 0x6d2b79f5;
    let value = state;
    value = Math.imul(value ^ (value >>> 15), value | 1);
    value ^= value + Math.imul(value ^ (value >>> 7), value | 61);
    return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
  };
}

function gaussian(random: Random) {
  const first = Math.max(random(), Number.EPSILON);
  const second = random();
  return Math.sqrt(-2 * Math.log(first)) * Math.cos(FULL_CIRCLE * second);
}

function particleColor(random: Random, normalizedRadius: number) {
  const choice = random();
  if (normalizedRadius < 0.18 || choice > 0.91) {
    return { hue: 42 + random() * 14, saturation: 74, lightness: 82 + random() * 12 };
  }
  if (choice > 0.76) {
    return { hue: 260 + random() * 48, saturation: 62, lightness: 72 + random() * 14 };
  }
  if (choice > 0.58) {
    return { hue: 0, saturation: 0, lightness: 82 + random() * 16 };
  }
  return { hue: 184 + random() * 38, saturation: 65, lightness: 72 + random() * 18 };
}

function createArmParticles(random: Random, count: number): GalaxyParticle[] {
  return Array.from({ length: count }, (_, index) => {
    const normalizedRadius = Math.pow(random(), 1.42);
    const radius = 10 + normalizedRadius * MAX_RADIUS;
    const arm = index % 4;
    const armWidth = 0.08 + normalizedRadius * 0.44;
    const angle = arm * (FULL_CIRCLE / 4) + radius * 0.0185 + gaussian(random) * armWidth;
    const color = particleColor(random, normalizedRadius);

    return {
      radius,
      angle,
      crossArm: gaussian(random) * (2 + normalizedRadius * 15),
      size: 0.42 + random() * 1.15 + (1 - normalizedRadius) * 0.72,
      alpha: 0.12 + random() * 0.42 + (1 - normalizedRadius) * 0.14,
      ...color,
      phase: random() * FULL_CIRCLE,
      twinkle: 0.45 + random() * 1.1,
      spin: 0.000009 + (1 - normalizedRadius) * 0.000008,
      drift: 0.8 + random() * 2.6,
    };
  });
}

function createCoreParticles(random: Random, count: number): GalaxyParticle[] {
  return Array.from({ length: count }, () => {
    const radius = Math.min(78, Math.abs(gaussian(random)) * 31);
    const color = particleColor(random, radius / MAX_RADIUS);
    return {
      radius,
      angle: random() * FULL_CIRCLE,
      crossArm: gaussian(random) * 7,
      size: 0.7 + random() * 1.7,
      alpha: 0.25 + random() * 0.42,
      ...color,
      phase: random() * FULL_CIRCLE,
      twinkle: 0.65 + random() * 1.3,
      spin: 0.000016 + random() * 0.000006,
      drift: 0.4 + random() * 1.3,
    };
  });
}

function createHaloParticles(random: Random, count: number): GalaxyParticle[] {
  return Array.from({ length: count }, () => {
    const normalizedRadius = Math.sqrt(random());
    const radius = 70 + normalizedRadius * (MAX_RADIUS + 18);
    const color = particleColor(random, normalizedRadius);
    return {
      radius,
      angle: random() * FULL_CIRCLE,
      crossArm: gaussian(random) * 30,
      size: 0.28 + random() * 0.72,
      alpha: 0.035 + random() * 0.1,
      ...color,
      phase: random() * FULL_CIRCLE,
      twinkle: 0.28 + random() * 0.68,
      spin: 0.000005 + random() * 0.000004,
      drift: 1.2 + random() * 3.8,
    };
  });
}

function createGalaxyParticles() {
  const random = seededRandom(0x6a11a7);
  return [
    ...createHaloParticles(random, 520),
    ...createArmParticles(random, 4480),
    ...createCoreParticles(random, 640),
  ];
}

const GALAXY_PARTICLES = createGalaxyParticles();

function drawCoreGlow(context: CanvasRenderingContext2D) {
  context.save();
  context.scale(1, 0.48);
  const glow = context.createRadialGradient(0, 0, 2, 0, 0, 165);
  glow.addColorStop(0, "rgba(255, 249, 214, 0.16)");
  glow.addColorStop(0.18, "rgba(212, 240, 255, 0.09)");
  glow.addColorStop(0.5, "rgba(116, 198, 230, 0.035)");
  glow.addColorStop(1, "rgba(70, 140, 190, 0)");
  context.fillStyle = glow;
  context.fillRect(-180, -180, 360, 360);
  context.restore();
}

function drawGalaxy(
  context: CanvasRenderingContext2D,
  particles: GalaxyParticle[],
  elapsed: number,
) {
  context.clearRect(0, 0, CANVAS_WIDTH, CANVAS_HEIGHT);
  context.save();
  context.scale(RENDER_SCALE, RENDER_SCALE);
  context.translate(CENTER_X, CENTER_Y);
  context.rotate(-0.1);
  context.globalCompositeOperation = "lighter";
  drawCoreGlow(context);

  particles.forEach((particle) => {
    const angle = particle.angle + elapsed * particle.spin;
    const radius = particle.radius + Math.sin(elapsed * 0.00018 + particle.phase) * particle.drift;
    const tangent = angle + Math.PI / 2;
    const x = Math.cos(angle) * radius + Math.cos(tangent) * particle.crossArm;
    const y = (Math.sin(angle) * radius + Math.sin(tangent) * particle.crossArm) * 0.48;
    const pulse = 0.78 + Math.sin(elapsed * 0.0012 * particle.twinkle + particle.phase) * 0.22;
    const alpha = Math.min(1, particle.alpha * pulse * PARTICLE_ALPHA_SCALE);
    const size = (particle.size / RENDER_SCALE) * (0.92 + pulse * 0.12);

    context.fillStyle = `hsla(${particle.hue} ${particle.saturation}% ${particle.lightness}% / ${alpha})`;
    if (size < 1.2) {
      context.fillRect(x, y, size, size);
      return;
    }

    context.beginPath();
    context.arc(x, y, size, 0, FULL_CIRCLE);
    context.fill();
  });

  context.restore();
}

export function ParticleGalaxy({ reducedMotion = false }: { reducedMotion?: boolean }) {
  const canvasRef = React.useRef<HTMLCanvasElement>(null);
  const { resolvedTheme } = useTheme();
  const enabled = resolvedTheme === "dark";

  React.useEffect(() => {
    const canvas = canvasRef.current;
    const context = canvas?.getContext("2d");
    if (!canvas || !context) return;

    if (!enabled) {
      context.clearRect(0, 0, CANVAS_WIDTH, CANVAS_HEIGHT);
      return;
    }

    if (reducedMotion) {
      drawGalaxy(context, GALAXY_PARTICLES, 0);
      return;
    }

    let animationFrame = 0;
    let lastFrame = 0;
    const startTime = performance.now();

    const render = (now: number) => {
      if (now - lastFrame >= FRAME_INTERVAL) {
        drawGalaxy(context, GALAXY_PARTICLES, now - startTime);
        lastFrame = now;
      }
      animationFrame = window.requestAnimationFrame(render);
    };

    animationFrame = window.requestAnimationFrame(render);
    return () => window.cancelAnimationFrame(animationFrame);
  }, [enabled, reducedMotion]);

  return (
    <canvas
      ref={canvasRef}
      className="sag-space-galaxy"
      width={CANVAS_WIDTH}
      height={CANVAS_HEIGHT}
      data-particle-count={GALAXY_PARTICLES.length}
      aria-hidden
    />
  );
}
