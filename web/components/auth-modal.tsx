"use client";

import { useEffect, useState } from "react";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type AuthUser = { user_id: string; email: string };

type Props = {
  open: boolean;
  onClose: () => void;
  onSuccess: (user: AuthUser, token: string) => void;
};

export function AuthModal({ open, onClose, onSuccess }: Props) {
  const [mode, setMode] = useState<"signup" | "login" | "magic">("signup");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [magicSent, setMagicSent] = useState(false);
  const [googleConfig, setGoogleConfig] = useState<{ enabled: boolean; client_id?: string; redirect_uri?: string } | null>(null);

  useEffect(() => {
    if (!open) return;
    fetch(`${API}/auth/google/config`)
      .then((r) => r.json())
      .then(setGoogleConfig)
      .catch(() => setGoogleConfig({ enabled: false }));
  }, [open]);

  function startGoogle() {
    if (!googleConfig?.enabled || !googleConfig.client_id) return;
    const redirectUri = window.location.origin + "/auth/google";
    const url = new URL("https://accounts.google.com/o/oauth2/v2/auth");
    url.searchParams.set("client_id", googleConfig.client_id);
    url.searchParams.set("redirect_uri", redirectUri);
    url.searchParams.set("response_type", "code");
    url.searchParams.set("scope", "openid email");
    url.searchParams.set("access_type", "online");
    url.searchParams.set("prompt", "select_account");
    window.location.href = url.toString();
  }

  if (!open) return null;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!email) {
      setError("Email required");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      if (mode === "magic") {
        const r = await fetch(`${API}/auth/magic-link`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ email }),
        });
        if (!r.ok) throw new Error("Could not send link");
        setMagicSent(true);
        return;
      }
      if (!password) {
        setError("Password required");
        return;
      }
      if (password.length < 8) {
        setError("Password must be ≥8 characters");
        return;
      }
      const r = await fetch(`${API}/auth/${mode}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      });
      const data = await r.json();
      if (!r.ok) {
        throw new Error(data.detail || "Request failed");
      }
      localStorage.setItem("rw_token", data.token);
      localStorage.setItem("rw_user", JSON.stringify({ user_id: data.user_id, email: data.email }));
      onSuccess({ user_id: data.user_id, email: data.email }, data.token);
      setPassword("");
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
      onClick={onClose}
    >
      <div
        className="bg-white rounded-lg shadow-xl p-6 w-full max-w-sm"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold">
            {mode === "signup" ? "Create account" : mode === "magic" ? "Email me a sign-in link" : "Sign in"}
          </h2>
          <button
            className="text-stone-400 hover:text-stone-700 text-2xl leading-none"
            onClick={onClose}
            aria-label="Close"
          >
            ×
          </button>
        </div>
        {magicSent ? (
          <div className="space-y-3 text-sm">
            <div className="text-emerald-700 font-medium">📬 Check your inbox</div>
            <p className="text-stone-600 leading-relaxed">
              We sent a sign-in link to <strong>{email}</strong>. Click it from
              the same browser. The link expires in 15 minutes.
            </p>
            <button
              onClick={() => {
                setMagicSent(false);
                setMode("login");
              }}
              className="text-emerald-700 hover:underline text-xs"
            >
              Use a different method
            </button>
          </div>
        ) : (
          <>
            <p className="text-xs text-stone-500 mb-3">
              Save your preferences so they persist between sessions. We use
              this data to improve recommendations.
            </p>
            {googleConfig?.enabled && (
              <>
                <button
                  type="button"
                  onClick={startGoogle}
                  className="w-full py-2 mb-3 border border-stone-300 rounded text-sm font-medium hover:bg-stone-50 flex items-center justify-center gap-2"
                >
                  <svg width="16" height="16" viewBox="0 0 18 18" aria-hidden="true">
                    <path fill="#4285F4" d="M17.64 9.2c0-.64-.06-1.25-.16-1.84H9v3.49h4.84c-.21 1.13-.84 2.09-1.79 2.73v2.27h2.89c1.69-1.56 2.67-3.86 2.67-6.65z" />
                    <path fill="#34A853" d="M9 18c2.43 0 4.46-.8 5.95-2.18l-2.89-2.27c-.8.54-1.83.86-3.06.86-2.34 0-4.33-1.58-5.04-3.71H.96v2.34A8.99 8.99 0 0 0 9 18z" />
                    <path fill="#FBBC05" d="M3.96 10.7c-.18-.54-.28-1.12-.28-1.7s.1-1.16.28-1.7V4.96H.96A9 9 0 0 0 0 9c0 1.45.35 2.83.96 4.04l3-2.34z" />
                    <path fill="#EA4335" d="M9 3.58c1.32 0 2.5.45 3.44 1.35l2.58-2.58A8.99 8.99 0 0 0 9 0 9 9 0 0 0 .96 4.96l3 2.34C4.67 5.17 6.66 3.58 9 3.58z" />
                  </svg>
                  Continue with Google
                </button>
                <div className="relative my-3">
                  <div className="absolute inset-0 flex items-center">
                    <div className="w-full border-t border-stone-200" />
                  </div>
                  <div className="relative flex justify-center text-xs">
                    <span className="bg-white px-2 text-stone-400">or</span>
                  </div>
                </div>
              </>
            )}
            <form onSubmit={submit} className="space-y-3">
              <div>
                <label className="block text-xs text-stone-600 mb-1">Email</label>
                <input
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  className="w-full px-3 py-2 border border-stone-300 rounded text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500"
                  autoFocus
                  required
                />
              </div>
              {mode !== "magic" && (
                <div>
                  <label className="block text-xs text-stone-600 mb-1">
                    Password{mode === "signup" ? " (≥8 chars)" : ""}
                  </label>
                  <input
                    type="password"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    className="w-full px-3 py-2 border border-stone-300 rounded text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500"
                    required
                    minLength={8}
                  />
                </div>
              )}
              {error && (
                <div className="text-xs text-red-700 bg-red-50 border border-red-200 px-3 py-2 rounded">
                  {error}
                </div>
              )}
              <button
                type="submit"
                disabled={busy}
                className="w-full py-2 bg-emerald-700 text-white rounded text-sm font-medium hover:bg-emerald-800 disabled:opacity-50"
              >
                {busy ? "…" : mode === "signup" ? "Sign up" : mode === "magic" ? "Send link" : "Sign in"}
              </button>
            </form>
            <div className="mt-3 space-y-1 text-center text-xs text-stone-500">
              <div>
                {mode === "signup"
                  ? "Already have an account? "
                  : mode === "magic"
                    ? "Have a password? "
                    : "New here? "}
                <button
                  type="button"
                  className="text-emerald-700 hover:underline"
                  onClick={() => {
                    setMode(mode === "signup" ? "login" : "signup");
                    setError(null);
                  }}
                >
                  {mode === "signup" || mode === "magic" ? "Sign in" : "Create one"}
                </button>
              </div>
              {mode !== "magic" && (
                <div>
                  Prefer passwordless?{" "}
                  <button
                    type="button"
                    className="text-emerald-700 hover:underline"
                    onClick={() => {
                      setMode("magic");
                      setError(null);
                    }}
                  >
                    Email me a sign-in link
                  </button>
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

export function useStoredUser(): {
  user: AuthUser | null;
  token: string | null;
  signOut: () => void;
  setAuth: (u: AuthUser, t: string) => void;
} {
  // SSR-safe: state initialized in useEffect
  const [user, setUser] = useState<AuthUser | null>(null);
  const [token, setToken] = useState<string | null>(null);

  if (typeof window !== "undefined" && user === null && token === null) {
    const t = localStorage.getItem("rw_token");
    const u = localStorage.getItem("rw_user");
    if (t && u) {
      try {
        const parsed = JSON.parse(u) as AuthUser;
        // Lazy-init: don't call setState during render. We'll defer to a microtask.
        Promise.resolve().then(() => {
          setUser(parsed);
          setToken(t);
        });
      } catch {
        // ignore
      }
    }
  }

  return {
    user,
    token,
    signOut: () => {
      localStorage.removeItem("rw_token");
      localStorage.removeItem("rw_user");
      setUser(null);
      setToken(null);
    },
    setAuth: (u, t) => {
      setUser(u);
      setToken(t);
    },
  };
}
