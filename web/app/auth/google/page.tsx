"use client";

import { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

function GoogleCallback() {
  const router = useRouter();
  const params = useSearchParams();
  const [status, setStatus] = useState<"checking" | "ok" | "error">("checking");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const code = params.get("code");
    const errParam = params.get("error");
    if (errParam) {
      setStatus("error");
      setError(errParam);
      return;
    }
    if (!code) {
      setStatus("error");
      setError("Missing authorization code from Google.");
      return;
    }
    (async () => {
      try {
        const redirectUri = window.location.origin + "/auth/google";
        const r = await fetch(`${API}/auth/google/exchange`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ code, redirect_uri: redirectUri }),
        });
        const data = await r.json();
        if (!r.ok) throw new Error(data.detail || "Google sign-in failed");
        localStorage.setItem("rw_token", data.token);
        localStorage.setItem(
          "rw_user",
          JSON.stringify({ user_id: data.user_id, email: data.email })
        );
        setStatus("ok");
        setTimeout(() => router.push("/"), 600);
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
            <p className="text-sm text-stone-500">Finishing sign-in with Google…</p>
          </>
        )}
        {status === "ok" && (
          <>
            <div className="text-2xl mb-2">✅</div>
            <p className="text-sm text-stone-500">Signed in. Redirecting…</p>
          </>
        )}
        {status === "error" && (
          <>
            <div className="text-2xl mb-2">⚠️</div>
            <h1 className="font-semibold mb-1">Google sign-in failed</h1>
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

export default function GoogleAuthPage() {
  return (
    <Suspense fallback={<div className="p-8 text-center">Loading…</div>}>
      <GoogleCallback />
    </Suspense>
  );
}
