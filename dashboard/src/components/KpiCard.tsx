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
    <div className={clsx("kpi", size === "lg" && "p-5", className)}>
      <div className="flex items-center justify-between">
        <span className={clsx("kpi-label", size === "lg" && "text-[13px]")}>
          {label}
        </span>
        {icon && <span className="text-muted">{icon}</span>}
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
    </div>
  );
}
