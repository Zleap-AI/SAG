"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "@/lib/api";
import type { FnOSNasStatus } from "@/lib/types";
import { useApp } from "@/components/features/app-shell";

export function useFnOSNasStatus() {
  const { capabilities } = useApp();
  const enabled = capabilities?.auth_mode === "fnos";
  const [status, setStatus] = useState<FnOSNasStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  const generation = useRef(0);

  const refresh = useCallback(async () => {
    if (!enabled) return null;
    const current = ++generation.current;
    setLoading(true);
    setError(null);
    try {
      const next = await api.fnosNasStatus();
      if (generation.current === current) setStatus(next);
      return next;
    } catch (value) {
      const nextError = value instanceof Error ? value : new Error("NAS access unavailable");
      if (generation.current === current) setError(nextError);
      return null;
    } finally {
      if (generation.current === current) setLoading(false);
    }
  }, [enabled]);

  useEffect(() => {
    if (!enabled) {
      setStatus(null);
      setError(null);
      setLoading(false);
      return;
    }
    void refresh();
    return () => {
      generation.current += 1;
    };
  }, [enabled, refresh]);

  return { enabled, status, loading, error, refresh };
}
