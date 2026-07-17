import type { ReactNode } from "react";
import { clsx } from "clsx";

export function KpiCard({
  label,
  value,
  hint,
  tone = "neutral",
  icon,
  className,
  size = "sm",
  trend,
  flashClassName,
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: "neutral" | "gain" | "loss" | "gold";
  icon?: ReactNode;
  className?: string;
  // "lg" is for hero/primary metrics in a bento layout (DESIGN.md:
  // "destacar visualmente las metricas principales... de las
  // secundarias") — bigger value type, more breathing room.
  size?: "sm" | "lg";
  // Optional understated trend-context visual (see MiniTrend), rendered
  // in the card's bottom-right corner behind/beside the value rather
  // than competing with it. Opt-in: callers decide whether their number
  // is live-refreshing enough to justify one.
  trend?: ReactNode;
  // Optional Tailwind animation class (e.g. from useFlashOnChange) that
  // flashes the card's own background translucent green/red for ~1.2s
  // when the underlying value crosses from one poll to the next
  // (DESIGN.md's "efecto flash muy sutil... verde/rojo translucido").
  flashClassName?: string;
}) {
  const toneClass =
    tone === "gain"
      ? "text-gain"
      : tone === "loss"
        ? "text-loss"
        : tone === "gold"
          ? "text-gold"
          : "text-cream-50";
  return (
    <div
      className={clsx(
        "kpi group relative overflow-hidden transition-[transform,border-color] duration-200 ease-out",
        "hover:-translate-y-0.5 hover:border-gold/40",
        size === "lg" && "p-5",
        flashClassName,
        className,
      )}
    >
      <div className="flex items-center justify-between">
        <span className={clsx("kpi-label", size === "lg" && "text-[13px]")}>
          {label}
        </span>
        {icon && (
          <span className="text-muted transition-colors duration-200 group-hover:text-gold/70">
            {icon}
          </span>
        )}
      </div>
      <div
        className={clsx(
          "kpi-value",
          size === "lg" ? "text-4xl" : "text-2xl",
          toneClass,
        )}
      >
        {value}
      </div>
      {hint && (
        <div className={clsx("mt-0.5 text-muted", size === "lg" ? "text-xs" : "text-[11px]")}>
          {hint}
        </div>
      )}
      {trend && (
        <div className="pointer-events-none absolute bottom-1 right-1">
          {trend}
        </div>
      )}
    </div>
  );
}
