import type { ReactNode } from "react";
import { clsx } from "clsx";

export function KpiCard({
  label,
  value,
  hint,
  tone = "neutral",
  icon,
  className,
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: "neutral" | "gain" | "loss" | "gold";
  icon?: ReactNode;
  className?: string;
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
    <div className={clsx("kpi", className)}>
      <div className="flex items-center justify-between">
        <span className="kpi-label">{label}</span>
        {icon && <span className="text-muted">{icon}</span>}
      </div>
      <div className={clsx("kpi-value", toneClass)}>{value}</div>
      {hint && (
        <div className="mt-0.5 text-[11px] text-muted">{hint}</div>
      )}
    </div>
  );
}
