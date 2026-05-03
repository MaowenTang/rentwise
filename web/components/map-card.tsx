"use client";

// Mapbox ships its CSS as a separate file. Without it, .mapboxgl-canvas
// doesn't get its `position: absolute; left:0; top:0` rules and the
// canvas stacks at its intrinsic size instead of filling the container —
// resulting in a map that only paints in the top portion of its frame.
import "mapbox-gl/dist/mapbox-gl.css";
import { useEffect, useRef } from "react";

export type MapPin = {
  zpid: string;
  lat: number;
  lng: number;
  rank: number;   // 1-based, shown as pin label
  score: number;  // 0–100, drives pin color tier
};

type MapCardProps = {
  pins: MapPin[];
  activeZpid: string | null;
  onPinClick: (zpid: string) => void;
  onMapReady: (panTo: (lat: number, lng: number) => void) => void;
  onMapDestroy: () => void;
};

function pinColors(score: number): { bg: string; border: string } {
  if (score >= 70) return { bg: "#10b981", border: "#059669" }; // emerald
  if (score >= 40) return { bg: "#f59e0b", border: "#d97706" }; // amber
  return { bg: "#9ca3af", border: "#6b7280" };                  // gray
}

function applyPinStyle(
  el: HTMLElement,
  rank: number,
  score: number,
  active: boolean
) {
  const { bg, border } = pinColors(score);
  const size = active ? 40 : 32;
  el.style.cssText = `
    width: ${size}px;
    height: ${size}px;
    border-radius: 50%;
    background: ${bg};
    border: ${active ? "2.5px" : "1.5px"} solid ${border};
    display: flex;
    align-items: center;
    justify-content: center;
    color: white;
    font-size: ${active ? 14 : 12}px;
    font-weight: 700;
    font-family: ui-monospace, monospace;
    cursor: pointer;
    box-shadow: ${
      active
        ? "0 2px 8px rgba(0,0,0,0.35)"
        : "0 1px 3px rgba(0,0,0,0.2)"
    };
    z-index: ${active ? 10 : 1};
    transition: all 0.15s ease;
    user-select: none;
    position: relative;
  `;
  el.textContent = String(rank);
}

export default function MapCard({
  pins,
  activeZpid,
  onPinClick,
  onMapReady,
  onMapDestroy,
}: MapCardProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const mapRef = useRef<any>(null);
  // zpid → { DOM element, rank, score } for reactive style updates
  const pinEls = useRef<Map<string, { el: HTMLElement; rank: number; score: number }>>(
    new Map()
  );

  // Stable refs for callbacks/data used inside the one-time init effect
  const pinsSnapshot = useRef(pins);
  const onPinClickRef = useRef(onPinClick);
  const onMapReadyRef = useRef(onMapReady);
  const onMapDestroyRef = useRef(onMapDestroy);
  useEffect(() => { onPinClickRef.current = onPinClick; }, [onPinClick]);
  useEffect(() => { onMapReadyRef.current = onMapReady; }, [onMapReady]);
  useEffect(() => { onMapDestroyRef.current = onMapDestroy; }, [onMapDestroy]);

  // Initialize Mapbox GL JS map — runs once on mount
  useEffect(() => {
    if (!containerRef.current || pinsSnapshot.current.length === 0) return;

    let cancelled = false;

    async function init() {
      const mapboxgl = (await import("mapbox-gl")).default;
      if (cancelled || !containerRef.current) return;

      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (mapboxgl as any).accessToken = process.env.NEXT_PUBLIC_MAPBOX_TOKEN!;

      const allLngs = pinsSnapshot.current.map((p) => p.lng);
      const allLats = pinsSnapshot.current.map((p) => p.lat);

      const map = new mapboxgl.Map({
        container: containerRef.current,
        style: "mapbox://styles/mapbox/streets-v12",
        bounds: [
          [Math.min(...allLngs), Math.min(...allLats)],
          [Math.max(...allLngs), Math.max(...allLats)],
        ],
        fitBoundsOptions: { padding: 60, maxZoom: 14 },
      });

      mapRef.current = map;

      // Mapbox sizes its canvas at construction time using the container's
      // bounding rect. When the component mounts inside a flex/grid parent
      // or with a delayed minHeight, the rect can be 0×N or N×0 on first
      // paint — leaving the canvas frozen at that tiny size even after
      // the container grows. Two safety nets:
      //   1. Force a resize once on first paint (covers most cases).
      //   2. ResizeObserver on the container — anytime layout shifts the
      //      box, Mapbox re-projects to fill it. Cheap and idempotent.
      requestAnimationFrame(() => {
        if (!cancelled) mapRef.current?.resize();
      });
      let resizeObserver: ResizeObserver | null = null;
      if (containerRef.current && typeof ResizeObserver !== "undefined") {
        resizeObserver = new ResizeObserver(() => {
          mapRef.current?.resize();
        });
        resizeObserver.observe(containerRef.current);
      }
      // Stash on the map so the cleanup closure can disconnect it
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (map as any).__rwResizeObserver = resizeObserver;

      map.on("load", () => {
        if (cancelled) return;

        // Re-resize after style load too — Mapbox sometimes recomputes
        // internal layout on first style application.
        map.resize();

        for (const pin of pinsSnapshot.current) {
          const el = document.createElement("div");
          applyPinStyle(el, pin.rank, pin.score, false);
          el.addEventListener("click", () => onPinClickRef.current(pin.zpid));

          new mapboxgl.Marker({ element: el, anchor: "center" })
            .setLngLat([pin.lng, pin.lat])
            .addTo(map);

          pinEls.current.set(pin.zpid, { el, rank: pin.rank, score: pin.score });
        }

        // Expose panTo so ShortlistCard can drive map navigation
        onMapReadyRef.current((lat: number, lng: number) => {
          mapRef.current?.flyTo({ center: [lng, lat], speed: 1.5 });
        });
      });
    }

    init().catch(console.error);

    return () => {
      cancelled = true;
      onMapDestroyRef.current();
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const ro = (mapRef.current as any)?.__rwResizeObserver as ResizeObserver | null | undefined;
      ro?.disconnect();
      mapRef.current?.remove();
      mapRef.current = null;
      pinEls.current.clear();
    };
  // Intentionally run only once on mount — pins come from pinsSnapshot ref
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Reactively update pin active styles whenever activeZpid changes
  useEffect(() => {
    pinEls.current.forEach(({ el, rank, score }, zpid) => {
      applyPinStyle(el, rank, score, zpid === activeZpid);
    });
  }, [activeZpid]);

  // Layout notes
  // ------------
  // Container sizing must be deterministic — Mapbox sizes its canvas from
  // getBoundingClientRect() at init time, so any "lazy" parent constraint
  // (minHeight, flex stretch, flex-basis: auto) that resolves AFTER mount
  // leaves the canvas pinned to the wrong size. We use a fixed pixel
  // height (no minHeight, no flex behavior) and ensure the inner div is
  // the direct sized parent for `new mapboxgl.Map({ container })`.
  //
  // `block` (not flex) on the outer + a single child that fills it
  // avoids stretch surprises from any ancestor flex column.
  return (
    <div
      className="block mt-2 w-full rounded-lg overflow-hidden border border-stone-200 shadow-sm bg-stone-100"
      style={{ height: 380 }}
    >
      <div
        ref={containerRef}
        style={{ width: "100%", height: "100%" }}
      />
    </div>
  );
}
