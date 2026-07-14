"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { clsx } from "clsx";
import { useAuth } from "@/lib/auth-context";
import { useLive } from "@/lib/use-live";
import type { WsStatus } from "@/lib/use-live";

const NAV = [
  { href: "/", label: "Overview", icon: "◈" },
  { href: "/positions", label: "Positions", icon: "◉" },
  { href: "/charts", label: "Charts", icon: "📈" },
  { href: "/history", label: "History", icon: "▦" },
  { href: "/audit", label: "Audit", icon: "▤" },
  { href: "/allocation", label: "Allocation & Risk", icon: "◊" },
  { href: "/settings", label: "Settings", icon: "⚙" },
];

function statusDot(s: WsStatus) {
  const color =
    s === "open"
      ? "bg-gain shadow-gain/40"
      : s === "connecting"
        ? "bg-gold"
        : s === "error"
          ? "bg-loss"
          : "bg-muted";
  return (
    <span
      className={clsx(
        "inline-block h-2 w-2 rounded-full",
        color,
        s === "open" && "shadow-[0_0_6px_currentColor]",
      )}
      title={`Live feed: ${s}`}
    />
  );
}

export function Sidebar() {
  const pathname = usePathname();
  const { logout, authenticated } = useAuth();
  const { status } = useLive({ autoReconnect: authenticated });

  return (
    <aside className="flex h-screen w-60 shrink-0 flex-col border-r border-ink-700 bg-ink-900/60 backdrop-blur">
      <div className="flex items-center gap-2 border-b border-ink-700 px-5 py-4">
        <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-gold/15 text-gold">
          <span className="text-lg font-bold">G</span>
        </div>
        <div>
          <div className="text-sm font-semibold leading-tight">Guaritradbot</div>
          <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-wider text-muted">
            {statusDot(status)}
            <span>Trading desk</span>
          </div>
        </div>
      </div>

      <nav className="flex-1 space-y-0.5 px-3 py-4">
        {NAV.map((item) => {
          const active =
            item.href === "/"
              ? pathname === "/"
              : pathname.startsWith(item.href);
          return (
            <Link
              key={item.href}
              href={item.href}
              className={clsx(
                "group flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition",
                active
                  ? "bg-gold/10 text-gold"
                  : "text-cream-50/80 hover:bg-ink-800 hover:text-cream-50",
              )}
            >
              <span
                className={clsx(
                  "text-base transition",
                  active ? "text-gold" : "text-muted group-hover:text-cream-50",
                )}
              >
                {item.icon}
              </span>
              <span>{item.label}</span>
            </Link>
          );
        })}
      </nav>

      <div className="border-t border-ink-700 p-3">
        <button
          onClick={logout}
          className="btn-ghost w-full justify-start text-xs text-muted hover:text-loss"
        >
          <span>⎋</span>
          <span>Sign out</span>
        </button>
      </div>
    </aside>
  );
}
