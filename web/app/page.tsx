"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

type AgentId = "search" | "property" | "location" | "outreach";
type Sender = "user" | "agent" | "system";

type Message = {
  id: string;
  sender: Sender;
  agent?: AgentId;
  text: string;
  routerReason?: string;
  meta?: Record<string, unknown>;
  ts: number;
};

type OnboardingResult = {
  user_name: string;
  budget_max: number | null;
  beds_min: number | null;
  beds_max: number | null;
  pets: string[];
  commute: { name: string; address?: string; max_minutes?: number | null } | null;
  must_haves: string[];
  avoid: string[];
  importance_ranking: string[];
};

type Profile = {
  budget_max?: number | null;
  beds_min?: number | null;
  beds_max?: number | null;
  pets?: string[];
  must_haves?: string[];
  nice_to_haves?: string[];
  avoid?: string[];
  neighborhoods?: string[];
  commute?: { name: string; address?: string; max_minutes?: number | null } | null;
  notes?: string;
};

type ShortlistItem = {
  zpid: string;
  name: string;
  address: string;
  neighborhood: string | null;
  rent_min: number | null;
  rent_max: number | null;
  rent_by_bed: Record<string, { min: number | null; max: number | null }>;
  walk_score: number | null;
  transit_score: number | null;
  url: string;
  score: number | null;
  score_components: Record<string, number>;
  score_explanation: string;
  added_via: string;
};

const AGENTS: {
  id: AgentId;
  label: string;
  color: string;
  badge: string;
  hint: string;
}[] = [
  { id: "search", label: "Search", color: "bg-emerald-500", badge: "🔍", hint: "Find listings" },
  { id: "property", label: "Property Analyst", color: "bg-sky-500", badge: "📋", hint: "Listing details" },
  { id: "location", label: "Location & Commute", color: "bg-violet-500", badge: "🗺️", hint: "Maps & schools" },
  { id: "outreach", label: "Outreach", color: "bg-amber-500", badge: "✉️", hint: "Email leasing offices" },
];

const AGENT_BY_ID: Record<AgentId, (typeof AGENTS)[number]> = AGENTS.reduce(
  (acc, a) => ({ ...acc, [a.id]: a }),
  {} as Record<AgentId, (typeof AGENTS)[number]>,
);

function uid() {
  return Math.random().toString(36).slice(2, 10);
}

export default function Home() {
  const sessionId = useMemo(() => uid() + uid(), []);
  const [messages, setMessages] = useState<Message[]>([
    {
      id: uid(),
      sender: "system",
      text: [
        "Welcome to **RentWise**. Just tell me what you're looking for in plain English — the agents will ask follow-up questions to fill in any gaps.",
        "",
        "_Try:_ `find me a place near Apple` — Search will ask about budget, then surface candidates and pin them to your shortlist on the right.",
      ].join("\n"),
      ts: Date.now(),
    },
  ]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [profileSummary, setProfileSummary] = useState<string>("(no preferences yet)");
  const [profile, setProfile] = useState<Profile>({});
  const [shortlist, setShortlist] = useState<ShortlistItem[]>([]);
  const [healthOk, setHealthOk] = useState<boolean | null>(null);
  const [keyOk, setKeyOk] = useState<boolean | null>(null);
  const [listingCount, setListingCount] = useState<number | null>(null);
  const [showOnboarding, setShowOnboarding] = useState(true);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    fetch(`${API}/healthz`)
      .then((r) => r.json())
      .then((d) => {
        setHealthOk(d.ok);
        setKeyOk(d.anthropic_key_present);
        setListingCount(d.listings_loaded ?? null);
      })
      .catch(() => setHealthOk(false));
  }, []);

  // Auto-scroll-to-bottom moved into <ChatScroll/> — it's smart now (only
  // sticks to bottom when the user is already near the bottom, so if you
  // scroll up to read history, it doesn't yank you back).

  async function send() {
    const text = input.trim();
    if (!text || busy) return;
    const userMsg: Message = { id: uid(), sender: "user", text, ts: Date.now() };
    setMessages((m) => [...m, userMsg]);
    setInput("");
    setBusy(true);
    try {
      const r = await fetch(`${API}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId, message: text }),
      });
      if (!r.ok) {
        const err = await r.text();
        throw new Error(err);
      }
      const data = await r.json();
      const agentMsg: Message = {
        id: uid(),
        sender: "agent",
        agent: data.agent as AgentId,
        text: data.reply,
        routerReason: data.router_reason,
        meta: data.metadata,
        ts: Date.now(),
      };
      setMessages((m) => [...m, agentMsg]);
      setProfileSummary(data.profile_summary || "(no preferences yet)");
      setProfile(data.profile || {});
      setShortlist(data.shortlist || []);
    } catch (e: unknown) {
      const errMsg = e instanceof Error ? e.message : String(e);
      setMessages((m) => [
        ...m,
        { id: uid(), sender: "system", text: `**Error:** ${errMsg}`, ts: Date.now() },
      ]);
    } finally {
      setBusy(false);
    }
  }

  async function removeFromShortlist(zpid: string) {
    try {
      const r = await fetch(`${API}/shortlist/remove`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId, zpid }),
      });
      const data = await r.json();
      setShortlist(data.shortlist || []);
    } catch {
      // ignore
    }
  }

  async function removeProfileItem(field: string, value: string | null) {
    try {
      const r = await fetch(`${API}/profile/remove`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId, field, value }),
      });
      const data = await r.json();
      setProfile(data.profile || {});
      setProfileSummary(data.profile_summary || "(no preferences yet)");
      setShortlist(data.shortlist || []);
    } catch {
      // ignore
    }
  }

  async function submitOnboarding(payload: OnboardingResult) {
    setBusy(true);
    try {
      const r = await fetch(`${API}/profile/init`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId, ...payload }),
      });
      if (!r.ok) {
        const err = await r.text();
        throw new Error(err);
      }
      const data = await r.json();
      setProfile(data.profile || {});
      setProfileSummary(data.profile_summary || "(no preferences yet)");
      setShortlist(data.shortlist || []);
      // Insert the synthetic search and its reply as visible chat turns
      if (data.initial_message) {
        setMessages((m) => [
          ...m,
          {
            id: uid(),
            sender: "user",
            text: data.initial_message.user,
            ts: Date.now(),
          },
          {
            id: uid(),
            sender: "agent",
            agent: data.initial_message.agent as AgentId,
            text: data.initial_message.reply,
            ts: Date.now(),
          },
        ]);
      }
      setShowOnboarding(false);
    } catch (e: unknown) {
      const errMsg = e instanceof Error ? e.message : String(e);
      setMessages((m) => [
        ...m,
        { id: uid(), sender: "system", text: `**Error during onboarding:** ${errMsg}`, ts: Date.now() },
      ]);
      setShowOnboarding(false);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="h-screen w-screen bg-stone-50 text-stone-900 grid grid-cols-[260px_1fr_400px] grid-rows-1 overflow-hidden font-sans relative">
      {showOnboarding && (
        <OnboardingQuestionnaire
          onSubmit={submitOnboarding}
          onSkip={() => setShowOnboarding(false)}
          busy={busy}
        />
      )}

      {/* Sidebar */}
      <aside className="border-r border-stone-200 flex flex-col min-h-0 h-full">
        <div className="px-4 py-4 border-b border-stone-200">
          <div className="text-lg font-semibold tracking-tight text-stone-900">
            Rent<span className="italic font-medium" style={{fontFamily:"ui-serif, 'Iowan Old Style', Georgia, serif"}}>Wise</span>
          </div>
          <div className="text-[11px] uppercase tracking-[0.12em] text-stone-500 mt-0.5">Bay Area · v0</div>
        </div>
        <nav className="flex-1 min-h-0 px-2 py-3 overflow-y-auto">
          <div className="text-xs uppercase tracking-wider text-stone-500 px-2 mb-2">
            Channels
          </div>
          <button className="w-full text-left px-3 py-1.5 rounded bg-stone-900 text-stone-50 text-sm font-medium">
            # general
          </button>

          <div className="text-xs uppercase tracking-wider text-stone-500 px-2 mt-5 mb-2">
            Agents in room
          </div>
          {AGENTS.map((a) => (
            <button
              key={a.id}
              onClick={() => setInput((s) => `@${a.id} ${s}`.trimEnd() + " ")}
              className="w-full flex items-center gap-2 px-3 py-1.5 rounded text-sm text-stone-800 hover:bg-stone-100/60 transition"
              title={`${a.hint} — click to @mention`}
            >
              <span className={`w-2 h-2 rounded-full ${a.color}`} />
              <span>{a.label}</span>
              <span className="ml-auto text-[10px] uppercase text-stone-400">
                @{a.id}
              </span>
            </button>
          ))}
          <button className="mt-2 w-full text-left px-3 py-1.5 rounded text-sm text-stone-500 hover:text-stone-700 hover:bg-stone-100/50">
            + Add agent
          </button>

          <div className="text-xs uppercase tracking-wider text-stone-500 px-2 mt-5 mb-2 flex items-center justify-between">
            <span>What I know about you</span>
            <button
              onClick={() => setShowOnboarding(true)}
              className="text-[10px] text-stone-500 hover:text-stone-900 normal-case underline-offset-2 hover:underline"
              title="Re-open the onboarding questionnaire"
            >
              edit
            </button>
          </div>
          <ProfileChips profile={profile} onRemove={removeProfileItem} />
        </nav>
        <div className="px-4 py-3 border-t border-stone-200 text-xs space-y-1">
          <div className="text-stone-500">
            {healthOk === null && "checking api…"}
            {healthOk === true && (
              <span className="text-emerald-700">● api ready</span>
            )}
            {healthOk === false && (
              <span className="text-red-700">● api unreachable</span>
            )}
          </div>
          <div>
            {keyOk === false && (
              <span className="text-amber-700">⚠ ANTHROPIC_API_KEY missing</span>
            )}
            {keyOk === true && <span className="text-emerald-700">key set</span>}
          </div>
        </div>
      </aside>

      {/* Main chat */}
      <main className="flex flex-col min-w-0 min-h-0 h-full">
        <header className="border-b border-stone-200 px-6 py-3 flex items-center gap-3 shrink-0">
          <div className="text-sm font-medium">#general</div>
          <div className="text-xs text-stone-500">
            4 agents
            {listingCount != null && ` · ${listingCount.toLocaleString()} Bay Area listings (Zillow + Craigslist)`}
            {" · "}live shortlist on the right
          </div>
        </header>

        <ChatScroll messages={messages} busy={busy} endRef={endRef} />

        <ChatInput input={input} setInput={setInput} onSend={send} busy={busy} />
      </main>

      {/* Right rail: live shortlist */}
      <ShortlistRail
        items={shortlist}
        profileSummary={profileSummary}
        onRemove={removeFromShortlist}
      />
    </div>
  );
}

function ChatScroll({
  messages,
  busy,
  endRef,
}: {
  messages: Message[];
  busy: boolean;
  endRef: React.RefObject<HTMLDivElement | null>;
}) {
  const scrollerRef = useRef<HTMLDivElement>(null);
  const [stickToBottom, setStickToBottom] = useState(true);

  function onScroll() {
    const el = scrollerRef.current;
    if (!el) return;
    // Distance from bottom — within 80px counts as "at bottom"
    const dist = el.scrollHeight - el.scrollTop - el.clientHeight;
    setStickToBottom(dist < 80);
  }

  useEffect(() => {
    if (stickToBottom) {
      endRef.current?.scrollIntoView({ behavior: "smooth" });
    }
    // Intentionally NOT a dep on stickToBottom: only re-stick when new
    // content arrives, never on scroll-induced state changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [messages, busy]);

  return (
    <div
      ref={scrollerRef}
      onScroll={onScroll}
      className="flex-1 min-h-0 overflow-y-auto px-6 py-6 space-y-5"
    >
      {messages.map((m) => (
        <MessageRow key={m.id} m={m} />
      ))}
      {busy && (
        <div className="text-sm text-stone-500 italic flex items-center gap-2">
          <span className="w-2 h-2 rounded-full bg-emerald-400 animate-pulse" />
          router is dispatching…
        </div>
      )}
      <div ref={endRef} />
      {!stickToBottom && (
        <button
          onClick={() => endRef.current?.scrollIntoView({ behavior: "smooth" })}
          className="sticky bottom-2 left-1/2 -translate-x-1/2 text-xs px-3 py-1 rounded-full bg-stone-100 border border-stone-300 text-stone-700 hover:bg-stone-200 transition shadow-lg"
        >
          ↓ jump to latest
        </button>
      )}
    </div>
  );
}

function ChatInput({
  input,
  setInput,
  onSend,
  busy,
}: {
  input: string;
  setInput: (v: string | ((s: string) => string)) => void;
  onSend: () => void;
  busy: boolean;
}) {
  const taRef = useRef<HTMLTextAreaElement>(null);
  const [mention, setMention] = useState<{
    open: boolean;
    query: string;
    start: number; // index in input where the '@' is
    selected: number;
  }>({ open: false, query: "", start: -1, selected: 0 });

  const matches = useMemo(() => {
    if (!mention.open) return [];
    const q = mention.query.toLowerCase();
    return AGENTS.filter((a) => a.id.startsWith(q) || a.label.toLowerCase().startsWith(q));
  }, [mention]);

  function detectMentionContext(value: string, caret: number) {
    // Walk backward from caret to find an unbroken token starting with '@'.
    let i = caret - 1;
    while (i >= 0) {
      const ch = value[i];
      if (ch === "@") {
        // Make sure '@' is preceded by start-of-string or whitespace/punct.
        if (i === 0 || /\s/.test(value[i - 1])) {
          return { start: i, query: value.slice(i + 1, caret) };
        }
        return null;
      }
      if (/\s/.test(ch)) return null;
      i--;
    }
    return null;
  }

  function onChange(e: React.ChangeEvent<HTMLTextAreaElement>) {
    const v = e.target.value;
    const caret = e.target.selectionStart ?? v.length;
    setInput(v);
    const ctx = detectMentionContext(v, caret);
    if (ctx) {
      setMention({ open: true, start: ctx.start, query: ctx.query, selected: 0 });
    } else if (mention.open) {
      setMention((m) => ({ ...m, open: false }));
    }
  }

  function applyMention(agentId: AgentId) {
    if (!taRef.current) return;
    const before = input.slice(0, mention.start);
    const caret = taRef.current.selectionStart ?? input.length;
    const after = input.slice(caret);
    const insertion = `@${agentId} `;
    const next = before + insertion + after;
    setInput(next);
    setMention({ open: false, query: "", start: -1, selected: 0 });
    requestAnimationFrame(() => {
      const pos = (before + insertion).length;
      taRef.current?.focus();
      taRef.current?.setSelectionRange(pos, pos);
    });
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (mention.open && matches.length > 0) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setMention((m) => ({ ...m, selected: (m.selected + 1) % matches.length }));
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setMention((m) => ({
          ...m,
          selected: (m.selected - 1 + matches.length) % matches.length,
        }));
        return;
      }
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        applyMention(matches[mention.selected].id);
        return;
      }
      if (e.key === "Escape") {
        setMention((m) => ({ ...m, open: false }));
        return;
      }
    }
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      onSend();
    }
  }

  return (
    <div className="border-t border-stone-200 p-4 relative">
      {mention.open && matches.length > 0 && (
        <div className="absolute bottom-[68px] left-4 right-4 max-w-md bg-white border border-stone-300 rounded-lg shadow-lg overflow-hidden z-20">
          <div className="px-3 py-1.5 text-[10px] uppercase tracking-wider text-stone-500 border-b border-stone-200">
            Mention an agent
          </div>
          {matches.map((a, i) => (
            <button
              key={a.id}
              onClick={() => applyMention(a.id)}
              onMouseEnter={() =>
                setMention((m) => ({ ...m, selected: i }))
              }
              className={`w-full flex items-center gap-3 px-3 py-2 text-sm text-left ${
                i === mention.selected ? "bg-stone-100" : "hover:bg-stone-100/60"
              }`}
            >
              <span className={`w-2 h-2 rounded-full ${a.color}`} />
              <span className="font-medium text-stone-900">@{a.id}</span>
              <span className="text-stone-500 text-xs">{a.label}</span>
              <span className="ml-auto text-stone-400 text-xs">{a.hint}</span>
            </button>
          ))}
          <div className="px-3 py-1 text-[10px] text-stone-400 border-t border-stone-200">
            ↑↓ navigate · enter / tab to select · esc to dismiss
          </div>
        </div>
      )}
      <div className="flex items-end gap-2 bg-white border border-stone-200 rounded-lg px-3 py-2 focus-within:border-stone-400 shadow-sm">
        <textarea
          ref={taRef}
          value={input}
          onChange={onChange}
          onKeyDown={onKeyDown}
          placeholder="Type @ to address a specific agent, or just describe what you want…"
          rows={1}
          className="flex-1 bg-transparent outline-none resize-none text-sm placeholder:text-stone-400 max-h-40"
        />
        <button
          onClick={onSend}
          disabled={busy || !input.trim()}
          className="text-xs px-3 py-1.5 rounded bg-stone-900 hover:bg-stone-700 disabled:bg-stone-200 disabled:text-stone-500 transition"
        >
          Send
        </button>
      </div>
    </div>
  );
}

// =====================================================================
//   OnboardingQuestionnaire — first-visit modal that captures basics
//   + drag-to-reorder importance ranking. The ranking maps directly to
//   RankingService component weights server-side.
// =====================================================================

const IMPORTANCE_FEATURES: { key: string; emoji: string; label: string; hint: string }[] = [
  { key: "budget",    emoji: "💰", label: "Affordability",     hint: "Stay well under my max budget" },
  { key: "commute",   emoji: "🚗", label: "Short commute",      hint: "Close to work / school / family" },
  { key: "amenities", emoji: "🛁", label: "Modern amenities",   hint: "Pool, gym, in-unit laundry, etc." },
  { key: "pets",      emoji: "🐾", label: "Pet-friendly",       hint: "Building accepts my pet(s)" },
  { key: "walkable",  emoji: "🏪", label: "Walkable",            hint: "Grocery, restaurants, cafes on foot" },
  { key: "transit",   emoji: "🚆", label: "Public transit",      hint: "BART/VTA/bus access nearby" },
];

function OnboardingQuestionnaire({
  onSubmit,
  onSkip,
  busy,
}: {
  onSubmit: (r: OnboardingResult) => void;
  onSkip: () => void;
  busy: boolean;
}) {
  const [step, setStep] = useState(1);
  const [name, setName] = useState("");
  const [budget, setBudget] = useState(3500);
  const [beds, setBeds] = useState<number | null>(1); // 0=studio, 1, 2, 3+
  const [commuteName, setCommuteName] = useState("");
  const [pets, setPets] = useState<string[]>([]);
  const [order, setOrder] = useState<string[]>(IMPORTANCE_FEATURES.map((f) => f.key));
  const [musts, setMusts] = useState<string[]>([]);
  const [avoids, setAvoids] = useState<string[]>([]);
  const [mustInput, setMustInput] = useState("");
  const [avoidInput, setAvoidInput] = useState("");

  function togglePet(p: string) {
    setPets((cur) => (cur.includes(p) ? cur.filter((x) => x !== p) : [...cur, p]));
  }

  function moveItem(from: number, to: number) {
    if (to < 0 || to >= order.length || from === to) return;
    setOrder((cur) => {
      const next = [...cur];
      const [m] = next.splice(from, 1);
      next.splice(to, 0, m);
      return next;
    });
  }

  function addChip(value: string, list: "must" | "avoid") {
    const v = value.trim();
    if (!v) return;
    if (list === "must") {
      setMusts((cur) => (cur.includes(v) ? cur : [...cur, v]));
      setMustInput("");
    } else {
      setAvoids((cur) => (cur.includes(v) ? cur : [...cur, v]));
      setAvoidInput("");
    }
  }

  function submit() {
    onSubmit({
      user_name: name.trim(),
      budget_max: budget || null,
      beds_min: beds,
      beds_max: beds === 3 ? null : beds, // "3+" means open-ended upper
      pets,
      commute: commuteName.trim() ? { name: commuteName.trim() } : null,
      must_haves: musts,
      avoid: avoids,
      importance_ranking: order,
    });
  }

  return (
    <div className="absolute inset-0 z-50 bg-stone-900/40 backdrop-blur-sm flex items-center justify-center p-6">
      <div className="bg-white border border-stone-200 rounded-xl shadow-2xl w-full max-w-2xl max-h-[90vh] overflow-y-auto">
        {/* Header */}
        <div className="px-6 pt-6 pb-3 border-b border-stone-100">
          <div className="flex items-baseline justify-between">
            <div>
              <div className="text-xl font-semibold tracking-tight text-stone-900">
                Welcome to Rent<span className="italic font-medium" style={{fontFamily:"ui-serif, Georgia, serif"}}>Wise</span>
              </div>
              <div className="text-sm text-stone-500 mt-0.5">
                A quick 30-second setup so the agents know what to look for.
              </div>
            </div>
            <button
              onClick={onSkip}
              className="text-xs text-stone-400 hover:text-stone-700 underline-offset-2 hover:underline"
            >
              skip — I&apos;ll just chat
            </button>
          </div>
          <div className="mt-4 flex gap-1.5">
            {[1, 2, 3].map((s) => (
              <div
                key={s}
                className={`h-1 flex-1 rounded-full transition ${
                  s <= step ? "bg-stone-900" : "bg-stone-200"
                }`}
              />
            ))}
          </div>
          <div className="text-[11px] uppercase tracking-wider text-stone-500 mt-2">
            Step {step} of 3 · {step === 1 ? "Quick basics" : step === 2 ? "What matters most to you" : "Anything else?"}
          </div>
        </div>

        {/* Body */}
        <div className="px-6 py-5 min-h-[320px]">
          {step === 1 && (
            <div className="space-y-5">
              <div>
                <label className="text-sm font-medium text-stone-700">Your name</label>
                <input
                  type="text"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="So agents can sign emails on your behalf"
                  className="mt-1 w-full px-3 py-2 border border-stone-200 rounded-md text-sm focus:outline-none focus:border-stone-900"
                />
              </div>

              <div>
                <label className="text-sm font-medium text-stone-700">
                  💰 Max monthly rent: <span className="font-mono font-semibold">${budget.toLocaleString()}</span>
                </label>
                <input
                  type="range"
                  min={1000}
                  max={8000}
                  step={50}
                  value={budget}
                  onChange={(e) => setBudget(Number(e.target.value))}
                  className="mt-2 w-full accent-stone-900"
                />
                <div className="flex justify-between text-[10px] text-stone-400 mt-0.5 font-mono">
                  <span>$1,000</span><span>$3,000</span><span>$5,000</span><span>$8,000+</span>
                </div>
              </div>

              <div>
                <label className="text-sm font-medium text-stone-700">🛏️ Bedrooms</label>
                <div className="mt-1.5 flex gap-1.5">
                  {[
                    { v: 0, l: "Studio" },
                    { v: 1, l: "1BR" },
                    { v: 2, l: "2BR" },
                    { v: 3, l: "3BR+" },
                  ].map((b) => (
                    <button
                      key={b.v}
                      onClick={() => setBeds(b.v)}
                      className={`px-3 py-1.5 rounded-md text-sm border transition ${
                        beds === b.v
                          ? "bg-stone-900 text-white border-stone-900"
                          : "bg-white text-stone-700 border-stone-200 hover:border-stone-400"
                      }`}
                    >
                      {b.l}
                    </button>
                  ))}
                </div>
              </div>

              <div>
                <label className="text-sm font-medium text-stone-700">📍 Where do you commute to most?</label>
                <input
                  type="text"
                  value={commuteName}
                  onChange={(e) => setCommuteName(e.target.value)}
                  placeholder="e.g. Apple, Google, downtown SJ, Stanford"
                  className="mt-1 w-full px-3 py-2 border border-stone-200 rounded-md text-sm focus:outline-none focus:border-stone-900"
                />
                <div className="text-[11px] text-stone-400 mt-1">
                  We&apos;ll use straight-line distance for v0 — known employers (Apple, Google, Meta, etc.) auto-resolve to coordinates.
                </div>
              </div>

              <div>
                <label className="text-sm font-medium text-stone-700">🐾 Pets</label>
                <div className="mt-1.5 flex gap-1.5">
                  {["dogs", "cats"].map((p) => (
                    <button
                      key={p}
                      onClick={() => togglePet(p)}
                      className={`px-3 py-1.5 rounded-md text-sm border transition capitalize ${
                        pets.includes(p)
                          ? "bg-stone-900 text-white border-stone-900"
                          : "bg-white text-stone-700 border-stone-200 hover:border-stone-400"
                      }`}
                    >
                      {p}
                    </button>
                  ))}
                  <button
                    onClick={() => setPets([])}
                    className={`px-3 py-1.5 rounded-md text-sm border transition ${
                      pets.length === 0
                        ? "bg-stone-100 text-stone-700 border-stone-300"
                        : "text-stone-400 border-stone-200 hover:text-stone-700"
                    }`}
                  >
                    No pets
                  </button>
                </div>
              </div>
            </div>
          )}

          {step === 2 && (
            <div>
              <p className="text-sm text-stone-600 mb-4">
                Drag to rank what matters <strong>most</strong> at the top.
                Higher rank = stronger weight in our matching score.
              </p>
              <div className="space-y-1.5">
                {order.map((key, i) => {
                  const f = IMPORTANCE_FEATURES.find((x) => x.key === key)!;
                  return (
                    <div
                      key={key}
                      draggable
                      onDragStart={(e) => {
                        e.dataTransfer.setData("text/plain", String(i));
                        e.dataTransfer.effectAllowed = "move";
                      }}
                      onDragOver={(e) => {
                        e.preventDefault();
                        e.dataTransfer.dropEffect = "move";
                      }}
                      onDrop={(e) => {
                        e.preventDefault();
                        const from = Number(e.dataTransfer.getData("text/plain"));
                        moveItem(from, i);
                      }}
                      className="flex items-center gap-3 px-3 py-2.5 bg-white border border-stone-200 rounded-md hover:border-stone-400 hover:shadow-sm cursor-grab active:cursor-grabbing transition"
                    >
                      <span className="text-stone-300 text-lg leading-none select-none">⋮⋮</span>
                      <span className="text-xs font-mono text-stone-400 w-5">{i + 1}.</span>
                      <span className="text-xl">{f.emoji}</span>
                      <div className="flex-1 min-w-0">
                        <div className="text-sm font-medium text-stone-900">{f.label}</div>
                        <div className="text-xs text-stone-500 truncate">{f.hint}</div>
                      </div>
                      <div className="flex flex-col gap-0.5">
                        <button
                          onClick={() => moveItem(i, i - 1)}
                          disabled={i === 0}
                          className="text-[10px] text-stone-400 hover:text-stone-900 disabled:opacity-30 leading-none"
                        >▲</button>
                        <button
                          onClick={() => moveItem(i, i + 1)}
                          disabled={i === order.length - 1}
                          className="text-[10px] text-stone-400 hover:text-stone-900 disabled:opacity-30 leading-none"
                        >▼</button>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {step === 3 && (
            <div className="space-y-5">
              <div>
                <label className="text-sm font-medium text-stone-700">
                  ✅ Must-haves <span className="text-stone-400 font-normal">(deal-makers)</span>
                </label>
                <div className="mt-1.5 flex flex-wrap gap-1.5">
                  {musts.map((m) => (
                    <span
                      key={m}
                      className="inline-flex items-center gap-1 px-2 py-0.5 bg-emerald-50 border border-emerald-300 text-emerald-800 rounded-full text-xs"
                    >
                      {m}
                      <button
                        onClick={() => setMusts((c) => c.filter((x) => x !== m))}
                        className="hover:text-red-700 leading-none"
                      >✕</button>
                    </span>
                  ))}
                </div>
                <input
                  type="text"
                  value={mustInput}
                  onChange={(e) => setMustInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      addChip(mustInput, "must");
                    }
                  }}
                  placeholder="e.g. in-unit laundry, parking, pool. Press Enter to add."
                  className="mt-2 w-full px-3 py-2 border border-stone-200 rounded-md text-sm focus:outline-none focus:border-stone-900"
                />
              </div>

              <div>
                <label className="text-sm font-medium text-stone-700">
                  ❌ Definitely avoid <span className="text-stone-400 font-normal">(deal-breakers)</span>
                </label>
                <div className="mt-1.5 flex flex-wrap gap-1.5">
                  {avoids.map((m) => (
                    <span
                      key={m}
                      className="inline-flex items-center gap-1 px-2 py-0.5 bg-rose-50 border border-rose-300 text-rose-800 rounded-full text-xs"
                    >
                      {m}
                      <button
                        onClick={() => setAvoids((c) => c.filter((x) => x !== m))}
                        className="hover:text-red-700 leading-none"
                      >✕</button>
                    </span>
                  ))}
                </div>
                <input
                  type="text"
                  value={avoidInput}
                  onChange={(e) => setAvoidInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      addChip(avoidInput, "avoid");
                    }
                  }}
                  placeholder="e.g. thin walls, no parking, far from transit. Press Enter to add."
                  className="mt-2 w-full px-3 py-2 border border-stone-200 rounded-md text-sm focus:outline-none focus:border-stone-900"
                />
              </div>

              <div className="text-xs text-stone-400 leading-relaxed pt-2 border-t border-stone-100">
                You can edit any of this later by clicking <span className="font-mono text-stone-600">edit</span> next to &ldquo;What I know about you&rdquo; in the sidebar, or by chatting with the agents directly.
              </div>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="px-6 py-4 border-t border-stone-100 flex items-center justify-between bg-stone-50/60">
          <button
            onClick={() => setStep((s) => Math.max(1, s - 1))}
            disabled={step === 1}
            className="text-sm text-stone-600 hover:text-stone-900 disabled:opacity-30"
          >
            ← Back
          </button>
          {step < 3 ? (
            <button
              onClick={() => setStep((s) => s + 1)}
              className="text-sm px-4 py-1.5 rounded-md bg-stone-900 text-white hover:bg-stone-700 transition"
            >
              Next →
            </button>
          ) : (
            <button
              onClick={submit}
              disabled={busy}
              className="text-sm px-4 py-1.5 rounded-md bg-stone-900 text-white hover:bg-stone-700 disabled:bg-stone-300 transition"
            >
              {busy ? "Finding matches…" : "Get my matches →"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function ProfileChips({
  profile,
  onRemove,
}: {
  profile: Profile;
  onRemove: (field: string, value: string | null) => void;
}) {
  const chips: { field: string; value: string | null; label: string; tone: string }[] = [];

  if (profile.budget_max) {
    chips.push({
      field: "budget_max",
      value: null,
      label: `≤ $${profile.budget_max.toLocaleString()}`,
      tone: "emerald",
    });
  }
  if (profile.beds_min !== null && profile.beds_min !== undefined) {
    const b =
      profile.beds_min === profile.beds_max
        ? profile.beds_min === 0
          ? "studio"
          : `${profile.beds_min}BR`
        : `${profile.beds_min ?? "?"}-${profile.beds_max ?? "?"}BR`;
    chips.push({ field: "beds", value: null, label: b, tone: "emerald" });
  }
  for (const p of profile.pets ?? []) {
    chips.push({ field: "pets", value: p, label: `🐾 ${p}`, tone: "amber" });
  }
  if (profile.commute) {
    chips.push({
      field: "commute",
      value: null,
      label: `→ ${profile.commute.name}${
        profile.commute.max_minutes ? ` (≤${profile.commute.max_minutes}m)` : ""
      }`,
      tone: "violet",
    });
  }
  for (const n of profile.neighborhoods ?? []) {
    chips.push({ field: "neighborhoods", value: n, label: `📍 ${n}`, tone: "sky" });
  }
  for (const m of profile.must_haves ?? []) {
    chips.push({ field: "must_haves", value: m, label: `must: ${m}`, tone: "emerald" });
  }
  for (const m of profile.nice_to_haves ?? []) {
    chips.push({ field: "nice_to_haves", value: m, label: `nice: ${m}`, tone: "sky" });
  }
  for (const m of profile.avoid ?? []) {
    chips.push({ field: "avoid", value: m, label: `avoid: ${m}`, tone: "rose" });
  }

  if (chips.length === 0) {
    return (
      <div className="px-3 text-xs text-stone-500 italic bg-stone-100/60 border border-stone-200 rounded-md py-2">
        nothing yet — agents fill this in as you chat
      </div>
    );
  }

  const toneClass: Record<string, string> = {
    emerald: "bg-emerald-50 border-emerald-300 text-emerald-800",
    sky: "bg-sky-50 border-sky-300 text-sky-800",
    violet: "bg-violet-50 border-violet-300 text-violet-800",
    amber: "bg-amber-50 border-amber-300 text-amber-800",
    rose: "bg-rose-50 border-rose-300 text-rose-800",
  };

  return (
    <div className="px-2 flex flex-wrap gap-1.5">
      {chips.map((c, i) => (
        <span
          key={`${c.field}-${c.value}-${i}`}
          className={`group inline-flex items-center gap-1 px-2 py-0.5 rounded-full border text-[11px] ${toneClass[c.tone]}`}
        >
          <span className="truncate max-w-[140px]">{c.label}</span>
          <button
            onClick={() => onRemove(c.field, c.value)}
            className="opacity-50 group-hover:opacity-100 hover:text-red-700 transition leading-none"
            title="Remove — the agent won't use this anymore"
          >
            ✕
          </button>
        </span>
      ))}
    </div>
  );
}

function ShortlistRail({
  items,
  profileSummary,
  onRemove,
}: {
  items: ShortlistItem[];
  profileSummary: string;
  onRemove: (zpid: string) => void;
}) {
  return (
    <aside className="border-l border-stone-200 flex flex-col min-h-0 h-full bg-stone-50">
      <div className="px-4 py-3 border-b border-stone-200 flex items-baseline justify-between">
        <div>
          <div className="text-sm font-semibold tracking-tight">Your shortlist</div>
          <div className="text-xs text-stone-500">
            {items.length === 0 ? "no listings yet" : `${items.length} ranked by your prefs`}
          </div>
        </div>
        <div
          className="text-[10px] uppercase tracking-wider text-stone-400"
          title={`Currently ranking against: ${profileSummary}`}
        >
          live
        </div>
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto px-3 py-3 space-y-2">
        {items.length === 0 ? (
          <div className="text-sm text-stone-500 leading-relaxed px-2 pt-4">
            <p>Tell the Search Agent what you&apos;re looking for and matches will appear here, ranked live by your preferences.</p>
            <p className="text-xs text-stone-400 mt-3">
              Each new prompt refines your profile and re-sorts the list.
            </p>
          </div>
        ) : (
          items.map((it, i) => (
            <ShortlistCard
              key={it.zpid}
              item={it}
              rank={i + 1}
              onRemove={() => onRemove(it.zpid)}
            />
          ))
        )}
      </div>
    </aside>
  );
}

function ShortlistCard({
  item,
  rank,
  onRemove,
}: {
  item: ShortlistItem;
  rank: number;
  onRemove: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const score = item.score ?? 0;
  const scoreColor =
    score >= 85
      ? "text-emerald-700 border-emerald-300 bg-emerald-50"
      : score >= 70
        ? "text-sky-700 border-sky-300 bg-sky-50"
        : "text-stone-600 border-stone-200";

  const beds = Object.keys(item.rent_by_bed).join(", ") || "?";
  const rent =
    item.rent_min && item.rent_max
      ? `$${item.rent_min.toLocaleString()}–$${item.rent_max.toLocaleString()}`
      : "rent ?";

  const components = Object.entries(item.score_components || {})
    .sort((a, b) => b[1] - a[1]);

  return (
    <div className="border border-stone-200 rounded-lg bg-white overflow-hidden hover:border-stone-300 hover:shadow-md transition shadow-[0_1px_2px_rgba(28,25,23,0.04)]">
      <div className="px-3 py-2 flex items-start gap-3">
        <div className="flex flex-col items-center pt-0.5">
          <div className="text-[10px] text-stone-400 font-mono">#{rank}</div>
          <div
            className={`mt-1 w-9 h-9 rounded-md border flex items-center justify-center text-sm font-semibold ${scoreColor}`}
            title={item.score_explanation}
          >
            {Math.round(score)}
          </div>
        </div>
        <div className="flex-1 min-w-0">
          <div className="text-sm font-medium text-stone-900 truncate" title={item.name}>
            {item.name}
          </div>
          <div className="text-xs text-stone-500 truncate">
            {item.neighborhood || "—"} · {beds}
          </div>
          <div className="text-xs text-stone-600 mt-0.5">{rent}</div>
          <div className="flex items-center gap-2 mt-1.5 text-[10px] text-stone-500">
            {item.walk_score != null && <span>walk {item.walk_score}</span>}
            {item.transit_score != null && <span>transit {item.transit_score}</span>}
            <span className="ml-auto">via {item.added_via}</span>
          </div>
        </div>
        <button
          onClick={onRemove}
          className="text-stone-400 hover:text-red-700 text-sm leading-none px-1"
          title="Remove from shortlist"
        >
          ✕
        </button>
      </div>

      {components.length > 0 && (
        <div className="border-t border-stone-200/80">
          <button
            onClick={() => setExpanded((x) => !x)}
            className="w-full text-left px-3 py-1.5 text-[10px] uppercase tracking-wider text-stone-500 hover:text-stone-600 flex items-center justify-between"
          >
            <span>why?</span>
            <span>{expanded ? "−" : "+"}</span>
          </button>
          {expanded && (
            <div className="px-3 pb-2 space-y-1">
              {components.map(([k, v]) => (
                <div key={k} className="flex items-center gap-2 text-[11px]">
                  <span className="w-24 text-stone-500">{k}</span>
                  <div className="flex-1 h-1.5 bg-stone-100 rounded overflow-hidden">
                    <div
                      className={`h-full ${v >= 8 ? "bg-emerald-600" : v >= 5 ? "bg-sky-600" : "bg-stone-400"}`}
                      style={{ width: `${(v / 10) * 100}%` }}
                    />
                  </div>
                  <span className="w-8 text-right text-stone-600 font-mono">{v.toFixed(1)}</span>
                </div>
              ))}
              {item.url && (
                <a
                  href={item.url}
                  target="_blank"
                  rel="noreferrer noopener"
                  className="block text-[11px] text-emerald-700 hover:underline mt-2"
                >
                  Open on Zillow ↗
                </a>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function MessageRow({ m }: { m: Message }) {
  if (m.sender === "system") {
    return (
      <div className="text-sm text-stone-600 border-l-2 border-stone-300 pl-3 py-1 prose prose-stone prose-sm max-w-none">
        <Markdown text={m.text} />
      </div>
    );
  }

  const isUser = m.sender === "user";
  const a = m.agent ? AGENT_BY_ID[m.agent] : undefined;
  const accent = a?.color ?? "bg-stone-200";

  return (
    <div className="flex gap-3">
      <div
        className={`mt-0.5 w-8 h-8 rounded-md shrink-0 flex items-center justify-center text-xs font-semibold ${
          isUser ? "bg-stone-200" : accent
        }`}
      >
        {isUser ? "you" : a?.badge ?? "🤖"}
      </div>
      <div className="flex-1 min-w-0">
        <div className="flex items-baseline gap-2 mb-1 flex-wrap">
          <span className="text-sm font-medium text-stone-900">
            {isUser ? "You" : a?.label ?? "Agent"}
          </span>
          <span className="text-xs text-stone-400 font-mono">
            {new Date(m.ts).toLocaleTimeString([], {
              hour: "2-digit",
              minute: "2-digit",
            })}
          </span>
        </div>
        {isUser ? (
          <div className="text-sm text-stone-800 whitespace-pre-wrap leading-relaxed">
            <MentionText text={m.text} />
          </div>
        ) : (
          <div className="text-sm text-stone-800 prose prose-stone prose-sm max-w-none prose-a:text-emerald-700 prose-strong:text-stone-900">
            <Markdown text={m.text} />
          </div>
        )}
      </div>
    </div>
  );
}

const MENTION_RE = /@(search|property|location|outreach)\b/gi;

function MentionText({ text }: { text: string }) {
  const parts: Array<string | { agent: AgentId }> = [];
  let last = 0;
  for (const m of text.matchAll(MENTION_RE)) {
    if (m.index === undefined) continue;
    if (m.index > last) parts.push(text.slice(last, m.index));
    parts.push({ agent: m[1].toLowerCase() as AgentId });
    last = m.index + m[0].length;
  }
  if (last < text.length) parts.push(text.slice(last));

  return (
    <>
      {parts.map((p, i) => {
        if (typeof p === "string") return <span key={i}>{p}</span>;
        const a = AGENT_BY_ID[p.agent];
        return (
          <span
            key={i}
            className="inline-flex items-center gap-1 px-1.5 py-0.5 mx-0.5 rounded bg-stone-100 border border-stone-300 text-[12px] font-medium text-stone-900 align-baseline"
          >
            <span className={`w-1.5 h-1.5 rounded-full ${a.color}`} />
            @{p.agent}
          </span>
        );
      })}
    </>
  );
}

function Markdown({ text }: { text: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        a: (props) => <a {...props} target="_blank" rel="noreferrer noopener" />,
      }}
    >
      {text}
    </ReactMarkdown>
  );
}
