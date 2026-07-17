// DESIGN.md: "Loaders: Shimmers esqueleticos que calcan las dimensiones
// del contenedor final. Quedan prohibidos los spinners circulares
// genericos." These replace the page-level <PageSpinner/> gates with
// shapes that mirror the content about to render, so the layout
// doesn't jump and the page feels alive while data loads instead of
// showing a spinning circle on a blank screen.
import type { CSSProperties } from "react";

export function Skeleton({
  className = "",
  style,
}: {
  className?: string;
  style?: CSSProperties;
}) {
  return (
    <div
      className={`animate-pulse rounded-md bg-ink-800 ${className}`}
      style={style}
      aria-hidden="true"
    />
  );
}

export function KpiCardSkeleton({ size = "sm" }: { size?: "sm" | "lg" }) {
  return (
    <div className={`kpi ${size === "lg" ? "p-5" : ""}`}>
      <div className="flex items-center justify-between">
        <Skeleton className="h-3 w-20" />
        <Skeleton className="h-4 w-4 rounded" />
      </div>
      <Skeleton className={size === "lg" ? "mt-2 h-9 w-36" : "mt-1 h-7 w-24"} />
      <Skeleton className="mt-1.5 h-2.5 w-28" />
    </div>
  );
}

export function KpiRowSkeleton({ count = 6 }: { count?: number }) {
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
      {Array.from({ length: count }).map((_, i) => (
        <KpiCardSkeleton key={i} />
      ))}
    </div>
  );
}

export function TableSkeleton({ rows = 4 }: { rows?: number }) {
  return (
    <div className="space-y-3">
      <div className="flex gap-4">
        <Skeleton className="h-3 w-16" />
        <Skeleton className="h-3 w-12" />
        <Skeleton className="h-3 w-16" />
        <Skeleton className="h-3 w-16" />
        <Skeleton className="ml-auto h-3 w-14" />
      </div>
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="flex items-center gap-4 border-t border-ink-700/60 pt-3">
          <Skeleton className="h-4 w-20" />
          <Skeleton className="h-4 w-14" />
          <Skeleton className="h-4 w-16" />
          <Skeleton className="h-4 w-16" />
          <Skeleton className="ml-auto h-4 w-16" />
        </div>
      ))}
    </div>
  );
}

export function CardSkeleton({ lines = 3 }: { lines?: number }) {
  return (
    <div className="space-y-2.5 p-4">
      {Array.from({ length: lines }).map((_, i) => (
        <Skeleton key={i} className="h-3.5" />
      ))}
    </div>
  );
}

export function ChartSkeleton() {
  return <Skeleton className="h-64 w-full" />;
}

export function FormSkeleton({ fields = 6 }: { fields?: number }) {
  return (
    <div className="grid gap-4 p-4 sm:grid-cols-2">
      {Array.from({ length: fields }).map((_, i) => (
        <div key={i} className="space-y-1.5">
          <Skeleton className="h-3 w-24" />
          <Skeleton className="h-9 w-full rounded-lg" />
        </div>
      ))}
    </div>
  );
}

export function PositionDetailSkeleton() {
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="space-y-2">
          <Skeleton className="h-7 w-32" />
          <Skeleton className="h-4 w-48" />
        </div>
        <Skeleton className="h-9 w-24 rounded-lg" />
      </div>
      <KpiRowSkeleton count={4} />
      <div className="card overflow-hidden">
        <div className="card-header">
          <Skeleton className="h-4 w-20" />
        </div>
        <div className="p-4">
          <ChartSkeleton />
        </div>
      </div>
    </div>
  );
}

export function OverviewPageSkeleton() {
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="space-y-2">
          <Skeleton className="h-7 w-40" />
          <Skeleton className="h-4 w-72" />
        </div>
        <div className="flex gap-3">
          <Skeleton className="h-9 w-28 rounded-lg" />
          <Skeleton className="h-9 w-24 rounded-lg" />
        </div>
      </div>
      <div className="grid gap-3 md:grid-cols-2">
        <KpiCardSkeleton size="lg" />
        <KpiCardSkeleton size="lg" />
      </div>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <KpiCardSkeleton />
        <KpiCardSkeleton />
        <KpiCardSkeleton />
        <KpiCardSkeleton />
      </div>
      <div className="card overflow-hidden">
        <div className="card-header">
          <Skeleton className="h-4 w-28" />
        </div>
        <div className="p-4">
          <TableSkeleton rows={3} />
        </div>
      </div>
      <div className="card overflow-hidden">
        <div className="card-header">
          <Skeleton className="h-4 w-24" />
        </div>
        <div className="p-4">
          <ChartSkeleton />
        </div>
      </div>
    </div>
  );
}
