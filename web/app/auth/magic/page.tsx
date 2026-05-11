"use client";

import { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

function MagicVerify() {
  const router = useRouter();
  const params = useSearchParams();
  const [status, setStatus] = useState<"checking" | "ok" | "error">("checking");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const token = params.get("token");
    if (!token) {
      setStatus("error");
      setError("Missing token. Open the link from your email.");
      return;
    }
    (async () => {
      try {
        const r = await fetch(`${API}/auth/magic-verify`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ token }),
        });
        const data = await r.json();
        if (!r.ok) {
          throw new Error(data.detail || "Verification failed");
        }
        localStorage.setItem("rw_token", data.token);
        localStorage.setItem(
          "rw_user",
          JSON.stringify({ user_id: data.user_id, email: data.email })
        );
        setStatus("ok");
        // Bounce to home after a beat
        setTimeout(() => router.push("/"), 800);
      } catch (e) {
        setStatus("error");
        setError((e as Error).message);
      }
    })();
  }, [params, router]);

  return (
    <div className="min-h-screen flex items-center justify-center p-8 bg-stone-50">
      <div className="max-w-md w-full bg-white rounded-lg shadow p-6 text-center">
        {status === "checking" && (
          <>
            <div className="text-2xl mb-2">🔐</div>
            <h1 className="font-semibold mb-1">Signing you in…</h1>
            <p className="text-sm text-stone-500">Verifying your magic link</p>
          </>
        )}
        {status === "ok" && (
          <>
            <div className="text-2xl mb-2">✅</div>
            <h1 className="font-semibold mb-1">Welcome to RentWise</h1>
            <p className="text-sm text-stone-500">Redirecting…</p>
          </>
        )}
        {status === "error" && (
          <>
            <div className="text-2xl mb-2">⚠️</div>
            <h1 className="font-semibold mb-1">Link not valid</h1>
            <p className="text-sm text-red-700 mb-4">{error}</p>
            <button
              className="px-4 py-2 bg-emerald-700 text-white rounded text-sm hover:bg-emerald-800"
              onClick={() => router.push("/")}
            >
              Back to RentWise
            </button>
          </>
        )}
      </div>
    </div>
  );
}

export default function MagicPage() {
  return (
    <Suspense fallback={<div className="p-8 text-center">Loading…</div>}>
      <MagicVerify />
    </Suspense>
  );
}
