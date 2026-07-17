"use client";

import { ReactNode } from "react";
import { usePathname } from "next/navigation";
import { AnimatePresence, motion } from "framer-motion";
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

// Route transitions: a restrained fade + slight vertical settle, not a
// playful slide/bounce — this is a trading terminal, not a marketing site.
// Keyed by pathname so AnimatePresence knows when to swap views. Motion is
// limited to transform/opacity per DESIGN.md's performance rule.
const routeVariants = {
  initial: { opacity: 0, y: 6 },
  animate: { opacity: 1, y: 0 },
  exit: { opacity: 0, y: -6 },
};

function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <main className="flex-1 overflow-y-auto scrollbar-thin">
        <div className="mx-auto w-full max-w-7xl px-6 py-6">
          <AnimatePresence mode="wait" initial={false}>
            <motion.div
              key={pathname}
              variants={routeVariants}
              initial="initial"
              animate="animate"
              exit="exit"
              transition={{ duration: 0.18, ease: "easeOut" }}
            >
              {children}
            </motion.div>
          </AnimatePresence>
        </div>
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
