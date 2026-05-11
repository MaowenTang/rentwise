"use client";

import { useEffect, useState } from "react";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

type MemoryDict = Record<string, string>;

type Props = {
  open: boolean;
  onClose: () => void;
};

function normalizeKey(k: string): string {
  return k.trim().toLowerCase().replace(/\s+/g, "_").slice(0, 32);
}

export function MemoryModal({ open, onClose }: Props) {
  const [memory, setMemory] = useState<MemoryDict>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [newKey, setNewKey] = useState("");
  const [newValue, setNewValue] = useState("");

  useEffect(() => {
    if (!open) return;
    setError(null);
    setBusy(true);
    const token = localStorage.getItem("rw_token");
    if (!token) {
      setError("Sign in to view memory");
      setBusy(false);
      return;
    }
    fetch(`${API}/auth/memory`, {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then((r) => r.json())
      .then((d) => setMemory(d.memory || {}))
      .catch((e) => setError(String(e)))
      .finally(() => setBusy(false));
  }, [open]);

  async function saveAll() {
    setBusy(true);
    setError(null);
    try {
      const token = localStorage.getItem("rw_token");
      const r = await fetch(`${API}/auth/memory`, {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({ memory }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || "Save failed");
      setMemory(d.memory || {});
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function deleteKey(k: string) {
    setBusy(true);
    setError(null);
    try {
      const token = localStorage.getItem("rw_token");
      const r = await fetch(`${API}/auth/memory/${encodeURIComponent(k)}`, {
        method: "DELETE",
        headers: { Authorization: `Bearer ${token}` },
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || "Delete failed");
      setMemory(d.memory || {});
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function addNewLocal() {
    const k = normalizeKey(newKey);
    const v = newValue.trim();
    if (!k || !v) {
      setError("Both key and value required");
      return;
    }
    setMemory((m) => ({ ...m, [k]: v }));
    setNewKey("");
    setNewValue("");
    setError(null);
  }

  if (!open) return null;

  const keys = Object.keys(memory).sort();

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
      onClick={onClose}
    >
      <div
        className="bg-white rounded-lg shadow-xl w-full max-w-lg flex flex-col"
        style={{ maxHeight: "85vh" }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between p-5 border-b border-stone-200 shrink-0">
          <div>
            <h2 className="text-lg font-semibold">My memory</h2>
            <p className="text-xs text-stone-500 mt-0.5">
              Durable facts the agents remember across sessions.
            </p>
          </div>
          <button
            onClick={onClose}
            aria-label="Close"
            className="text-stone-400 hover:text-stone-700 text-2xl leading-none"
          >
            ×
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-5">
          {busy && keys.length === 0 ? (
            <p className="text-sm text-stone-500">Loading…</p>
          ) : keys.length === 0 ? (
            <p className="text-sm text-stone-500">
              Nothing remembered yet. As you chat, durable facts (like
              your work, family makeup, or vehicle) will appear here.
              You can also add them manually below.
            </p>
          ) : (
            <ul className="space-y-2">
              {keys.map((k) => (
                <li
                  key={k}
                  className="flex items-start gap-2 p-2 rounded border border-stone-200 hover:border-stone-300"
                >
                  <div className="flex-1 min-w-0">
                    <div className="text-[10px] uppercase tracking-wide text-stone-500 mb-0.5">
                      {k.replace(/_/g, " ")}
                    </div>
                    <textarea
                      value={memory[k]}
                      onChange={(e) =>
                        setMemory((m) => ({ ...m, [k]: e.target.value }))
                      }
                      className="w-full text-sm border border-stone-200 rounded px-2 py-1 resize-none focus:outline-none focus:ring-1 focus:ring-emerald-500"
                      rows={1}
                    />
                  </div>
                  <button
                    onClick={() => deleteKey(k)}
                    disabled={busy}
                    className="text-stone-400 hover:text-red-700 text-lg leading-none p-1"
                    title="Delete"
                  >
                    ×
                  </button>
                </li>
              ))}
            </ul>
          )}

          <div className="mt-5 pt-4 border-t border-stone-200">
            <div className="text-[10px] uppercase tracking-wide text-stone-500 mb-2">
              Add a fact
            </div>
            <div className="flex gap-2">
              <input
                value={newKey}
                onChange={(e) => setNewKey(e.target.value)}
                placeholder="key (e.g. allergy)"
                className="w-32 text-xs border border-stone-300 rounded px-2 py-1 focus:outline-none focus:ring-1 focus:ring-emerald-500"
              />
              <input
                value={newValue}
                onChange={(e) => setNewValue(e.target.value)}
                placeholder="value"
                className="flex-1 text-xs border border-stone-300 rounded px-2 py-1 focus:outline-none focus:ring-1 focus:ring-emerald-500"
                onKeyDown={(e) => {
                  if (e.key === "Enter") addNewLocal();
                }}
              />
              <button
                onClick={addNewLocal}
                className="text-xs px-3 py-1 bg-stone-100 hover:bg-stone-200 rounded"
              >
                Add
              </button>
            </div>
            <p className="text-[10px] text-stone-400 mt-1">
              Press <kbd className="px-1 border rounded">Save</kbd> below
              to persist additions and edits.
            </p>
          </div>

          {error && (
            <div className="mt-3 text-xs text-red-700 bg-red-50 border border-red-200 px-3 py-2 rounded">
              {error}
            </div>
          )}
        </div>

        <div className="flex items-center gap-2 p-4 border-t border-stone-200 shrink-0">
          <p className="text-[10px] text-stone-400 flex-1">
            {keys.length} {keys.length === 1 ? "fact" : "facts"} · loaded
            on every chat
          </p>
          <button
            onClick={onClose}
            className="text-xs px-3 py-1.5 rounded hover:bg-stone-100"
          >
            Cancel
          </button>
          <button
            onClick={saveAll}
            disabled={busy}
            className="text-xs px-3 py-1.5 rounded bg-emerald-700 text-white hover:bg-emerald-800 disabled:opacity-50"
          >
            {busy ? "…" : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
}
