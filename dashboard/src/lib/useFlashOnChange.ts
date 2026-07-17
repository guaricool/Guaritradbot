"use client";

import { useEffect, useRef, useState } from "react";

/**
 * Detects when a live-updating number crosses from one poll value to the
 * next and returns a Tailwind animation class ("animate-pulse-gain" /
 * "animate-pulse-loss") for ~1.2s, then clears it back to "".
 *
 * DESIGN.md asks for exactly this: "Animacion de Cambios: efecto flash muy
 * sutil en las celdas de precio... verde translucido subidas, rojo
 * translucido bajadas" -- the `pulse-gain`/`pulse-loss` keyframes already
 * existed in tailwind.config.ts but nothing ever applied them; this hook
 * is the missing wiring, applied to the raw (untweened) value so the
 * flash fires once per real backend update rather than continuously
 * while `useAnimatedNumber` eases toward it.
 */
export function useFlashOnChange(
  value: number | null | undefined,
  durationMs = 1200,
): "" | "animate-pulse-gain" | "animate-pulse-loss" {
  const prevRef = useRef<number | null>(value ?? null);
  const [flash, setFlash] = useState<"" | "animate-pulse-gain" | "animate-pulse-loss">("");
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (value === null || value === undefined || Number.isNaN(value)) {
      return;
    }
    const prev = prevRef.current;
    if (prev !== null && value !== prev) {
      setFlash(value > prev ? "animate-pulse-gain" : "animate-pulse-loss");
      if (timeoutRef.current) clearTimeout(timeoutRef.current);
      timeoutRef.current = setTimeout(() => setFlash(""), durationMs);
    }
    prevRef.current = value;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);

  useEffect(() => {
    return () => {
      if (timeoutRef.current) clearTimeout(timeoutRef.current);
    };
  }, []);

  return flash;
}
