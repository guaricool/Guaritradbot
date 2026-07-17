"use client";

/**
 * LiveDot -- a small solid marker plus a radiating pulse ring, meant to be
 * passed as recharts' custom `dot` (or a manual overlay) at the LAST point
 * of a live-refreshing series. This is what makes a chart read as "this is
 * moving right now" in between poll ticks, instead of looking like a
 * static historical image.
 *
 * DESIGN.md constraints respected:
 * - No neon/purple glow (`sombras difusas con colores de acento
 *   brillantes o moradas` is explicitly banned) -- this uses a plain
 *   solid-color ring with a CSS opacity/scale fade, not a blurred
 *   drop-shadow glow.
 * - "Performance: animaciones limitadas a transform y opacity" -- the
 *   pulse keyframe (`live-ping`, defined in tailwind.config.ts) only
 *   animates `transform: scale()` and `opacity`, never layout-affecting
 *   properties, so it stays cheap even with several charts mounted at
 *   once (PositionTable renders one per open position).
 */
export function LiveDot({
  cx,
  cy,
  color,
}: {
  cx?: number;
  cy?: number;
  color: string;
}) {
  if (cx === undefined || cy === undefined || cx === null || cy === null) {
    return null;
  }
  return (
    <g>
      {/* Radiating ring -- scale+opacity keyframe only. transform-box is
          set to fill-box so `scale()` expands from the circle's own
          center instead of the SVG viewport's origin. */}
      <circle
        cx={cx}
        cy={cy}
        r={5}
        fill={color}
        fillOpacity={0.45}
        className="animate-live-ping"
        style={{ transformBox: "fill-box", transformOrigin: "center" }}
      />
      {/* Solid center dot, always visible, reads as "current price". */}
      <circle cx={cx} cy={cy} r={3} fill={color} stroke="#070a14" strokeWidth={1} />
    </g>
  );
}
