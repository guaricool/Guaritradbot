"use client";

import { AuditTable } from "@/components/AuditTable";

export default function AuditPage() {
  return (
    <div className="space-y-5 animate-fade-in">
      <header>
        <h1 className="font-display text-2xl font-semibold tracking-tight">
          Audit log
        </h1>
        <p className="text-sm text-muted">
          Every signal, gate decision, and trade event the bot recorded.
        </p>
      </header>
      <AuditTable initialLimit={250} />
    </div>
  );
}
