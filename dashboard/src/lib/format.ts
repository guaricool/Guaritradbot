// Pure formatting helpers — no React, no fetching. Easy to unit-test.

export function fmtUsd(
  n: number | null | undefined,
  opts: { signed?: boolean; decimals?: number } = {},
): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const { signed = false, decimals = 2 } = opts;
  const abs = Math.abs(n);
  const formatted = abs.toLocaleString("en-US", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
  if (n === 0) return signed ? `+$${formatted}` : `$${formatted}`;
  const sign = signed ? (n > 0 ? "+" : "−") : n < 0 ? "−" : "";
  return `${sign}$${formatted}`;
}

export function fmtPct(
  n: number | null | undefined,
  opts: { signed?: boolean; decimals?: number } = {},
): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const { signed = false, decimals = 2 } = opts;
  const v = n * 100;
  const formatted = Math.abs(v).toFixed(decimals);
  if (v === 0) return signed ? `+${formatted}%` : `${formatted}%`;
  const sign = signed ? (v > 0 ? "+" : "−") : v < 0 ? "−" : "";
  return `${sign}${formatted}%`;
}

export function fmtNum(
  n: number | null | undefined,
  decimals = 4,
): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return n.toLocaleString("en-US", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

export function pnlClass(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n) || n === 0) return "text-muted";
  return n > 0 ? "text-gain" : "text-loss";
}

export function fmtAge(hours: number | null | undefined): string {
  if (hours === null || hours === undefined) return "—";
  if (hours < 1) return `${Math.round(hours * 60)}m`;
  if (hours < 48) return `${hours.toFixed(1)}h`;
  return `${(hours / 24).toFixed(1)}d`;
}

export function fmtTimestamp(ts: number | null | undefined): string {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString("en-US", {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

export function fmtTimeOnly(ts: number | null | undefined): string {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleTimeString("en-US", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

export function fmtDateOnly(iso: string | null | undefined): string {
  if (!iso) return "—";
  return iso.slice(0, 10);
}
