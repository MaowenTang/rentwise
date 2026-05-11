"use client";

import { useState } from "react";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type AuthUser = { user_id: string; email: string };

type Props = {
  open: boolean;
  onClose: () => void;
  onSuccess: (user: AuthUser, token: string) => void;
};

export function AuthModal({ open, onClose, onSuccess }: Props) {
  const [mode, setMode] = useState<"signup" | "login">("signup");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!open) return null;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!email || !password) {
      setError("Email and password required");
      return;
    }
    if (password.length < 8) {
      setError("Password must be ≥8 characters");
      return;
    }
    setBusy(true);
    setError(null);
    try {
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
            {mode === "signup" ? "Create account" : "Sign in"}
          </h2>
          <button
            className="text-stone-400 hover:text-stone-700 text-2xl leading-none"
            onClick={onClose}
            aria-label="Close"
          >
            ×
          </button>
        </div>
        <p className="text-xs text-stone-500 mb-3">
          Save your preferences so they persist between sessions. We use this
          data to improve recommendations.
        </p>
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
            {busy ? "…" : mode === "signup" ? "Sign up" : "Sign in"}
          </button>
        </form>
        <div className="mt-3 text-center text-xs text-stone-500">
          {mode === "signup" ? "Already have an account? " : "New here? "}
          <button
            type="button"
            className="text-emerald-700 hover:underline"
            onClick={() => {
              setMode(mode === "signup" ? "login" : "signup");
              setError(null);
            }}
          >
            {mode === "signup" ? "Sign in" : "Create one"}
          </button>
        </div>
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
