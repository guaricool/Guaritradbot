"use client";

import { AuthProvider, useAuth } from "@/lib/auth-context";
import { Spinner } from "@/components/Spinner";
import { LoginForm } from "@/components/LoginForm";

function Gate() {
  const { ready, authenticated } = useAuth();
  if (!ready) {
    return (
      <div className="flex min-h-screen items-center justify-center text-muted">
        <Spinner className="h-5 w-5" />
      </div>
    );
  }
  if (authenticated) {
    if (typeof window !== "undefined") window.location.replace("/");
    return null;
  }
  return <LoginForm />;
}

export default function LoginPage() {
  return (
    <AuthProvider>
      <Gate />
    </AuthProvider>
  );
}
