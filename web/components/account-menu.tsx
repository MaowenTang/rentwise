"use client";

import { useEffect, useRef, useState } from "react";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

type Props = {
  email: string;
  onSignOut: () => void;
  onOpenMemory?: () => void;
};

export function AccountMenu({ email, onSignOut, onOpenMemory }: Props) {
  const [open, setOpen] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);

  // Click-outside to close
  useEffect(() => {
    if (!open) return;
    function onClick(e: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false);
        setConfirming(false);
      }
    }
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [open]);

  async function downloadData() {
    setBusy(true);
    setError(null);
    try {
      const token = localStorage.getItem("rw_token");
      if (!token) throw new Error("Not signed in");
      const r = await fetch(`${API}/auth/export`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!r.ok) throw new Error(`Export failed (${r.status})`);
      const data = await r.json();
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `rentwise-data-${data.user.email.replace(/[^a-z0-9]/gi, "_")}.json`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      setOpen(false);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function deleteAccount() {
    setBusy(true);
    setError(null);
    try {
      const token = localStorage.getItem("rw_token");
      if (!token) throw new Error("Not signed in");
      const r = await fetch(`${API}/auth/me`, {
        method: "DELETE",
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!r.ok) throw new Error(`Delete failed (${r.status})`);
      localStorage.removeItem("rw_token");
      localStorage.removeItem("rw_user");
      onSignOut();
      setOpen(false);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="relative" ref={rootRef}>
      <button
        onClick={() => setOpen(!open)}
        className="text-stone-600 hover:text-stone-900 text-xs flex items-center gap-1"
      >
        <span>{email}</span>
        <span className="text-stone-400">▾</span>
      </button>
      {open && (
        <div className="absolute right-0 top-full mt-1 w-56 bg-white border border-stone-200 rounded-md shadow-lg z-50 text-xs">
          {!confirming ? (
            <>
              {onOpenMemory && (
                <button
                  onClick={() => {
                    onOpenMemory();
                    setOpen(false);
                  }}
                  className="block w-full text-left px-3 py-2 hover:bg-stone-50"
                >
                  🧠 My memory
                </button>
              )}
              <button
                onClick={downloadData}
                disabled={busy}
                className="block w-full text-left px-3 py-2 hover:bg-stone-50 disabled:opacity-50"
              >
                📥 Download my data
              </button>
              <button
                onClick={() => {
                  onSignOut();
                  setOpen(false);
                }}
                className="block w-full text-left px-3 py-2 hover:bg-stone-50"
              >
                🚪 Sign out
              </button>
              <div className="border-t border-stone-200" />
              <button
                onClick={() => setConfirming(true)}
                className="block w-full text-left px-3 py-2 text-red-700 hover:bg-red-50"
              >
                🗑 Delete my account
              </button>
            </>
          ) : (
            <div className="p-3 space-y-2">
              <p className="text-stone-800 font-medium">Delete account?</p>
              <p className="text-stone-600 leading-snug">
                This permanently erases your profile and all chat history.
                Cannot be undone.
              </p>
              <div className="flex gap-2 pt-1">
                <button
                  onClick={deleteAccount}
                  disabled={busy}
                  className="flex-1 py-1.5 bg-red-700 text-white rounded hover:bg-red-800 disabled:opacity-50"
                >
                  {busy ? "..." : "Delete forever"}
                </button>
                <button
                  onClick={() => setConfirming(false)}
                  disabled={busy}
                  className="flex-1 py-1.5 bg-stone-100 text-stone-800 rounded hover:bg-stone-200"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
          {error && (
            <div className="px-3 py-2 text-red-700 bg-red-50 border-t border-red-200">
              {error}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
