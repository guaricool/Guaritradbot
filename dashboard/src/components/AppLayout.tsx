"use client";

import { ReactNode } from "react";
import { AuthProvider, useAuth } from "@/lib/auth-context";
import { Sidebar } from "./Sidebar";
import { Spinner } from "./Spinner";
import { LoginForm } from "./LoginForm";

function Gate({ children }: { children: ReactNode }) {
  const { ready, authenticated } = useAuth();
  if (!ready) {
    return (
      <div className="flex min-h-screen items-center justify-center text-muted">
        <Spinner className="h-5 w-5" />
      </div>
    );
  }
  if (!authenticated) {
    return <LoginForm />;
  }
  return <AppShell>{children}</AppShell>;
}

function AppShell({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <main className="flex-1 overflow-y-auto scrollbar-thin">
        <div className="mx-auto w-full max-w-7xl px-6 py-6">{children}</div>
      </main>
    </div>
  );
}

export function AppLayout({ children }: { children: ReactNode }) {
  return (
    <AuthProvider>
      <Gate>{children}</Gate>
    </AuthProvider>
  );
}
