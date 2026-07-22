"use client";

import * as React from "react";

import { normalizeAvatar } from "@/lib/avatar";
import { DEFAULT_AGENT_AVATAR } from "@/lib/branding";
import { cn } from "@/lib/utils";

type PetHeadSize = "sm" | "md" | "lg";

const HEAD_SIZES: Record<PetHeadSize, { className: string; scale: number }> = {
  sm: { className: "size-8", scale: 0.36 },
  md: { className: "size-10", scale: 0.45 },
  lg: { className: "size-16", scale: 0.72 },
};

export function petFaceStyle(value: string): React.CSSProperties {
  const length = Array.from(value).length;
  const emojiLike = /\p{Extended_Pictographic}/u.test(value);
  if (emojiLike && length <= 2) {
    return { fontFamily: "system-ui, sans-serif", fontSize: length === 1 ? 28 : 23 };
  }
  if (length <= 1) return { fontSize: 22 };
  if (length <= 3) return { fontSize: 18 };
  if (length <= 5) return { fontSize: 13 };
  return { fontSize: 10 };
}

export function PetHeadAvatar({
  face,
  size = "sm",
  className,
}: {
  face: string;
  size?: PetHeadSize;
  className?: string;
}) {
  const value = normalizeAvatar(face) || DEFAULT_AGENT_AVATAR;
  const config = HEAD_SIZES[size];
  const style = { "--pet-head-scale": config.scale } as React.CSSProperties;

  return (
    <span
      aria-hidden="true"
      className={cn("sag-pet-head-avatar shrink-0", config.className, className)}
      style={style}
    >
      <span className="sag-pet__bubble" aria-hidden />
      <span className="sag-pet__helmet">
        <span className="sag-pet__antenna" />
        <span className="sag-pet__visor">
          <span className="sag-pet__glass" />
          <span className="sag-pet__face" style={petFaceStyle(value)}>
            <span className="sag-pet__face-glyph">{value}</span>
          </span>
        </span>
        <span className="sag-pet__shine" />
      </span>
    </span>
  );
}
