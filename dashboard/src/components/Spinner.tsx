export function Spinner({ className = "" }: { className?: string }) {
  return (
    <div
      className={`inline-block h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent ${className}`}
      aria-label="Loading"
    />
  );
}

export function PageSpinner({ label = "Loading..." }: { label?: string }) {
  return (
    <div className="flex h-full min-h-[200px] items-center justify-center gap-3 text-muted">
      <Spinner className="h-5 w-5" />
      <span className="text-sm">{label}</span>
    </div>
  );
}
