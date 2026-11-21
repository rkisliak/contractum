// The Contractum seal: a 12-point rosette (every published contract passed the rules) with an inner
// ring and a check. Fills come from CSS variables set by the Logo tone (see Logo.module.css).
export const GLYPH = {
  rosette:
    "M16.00 2.00 L19.17 4.17 L23.00 3.88 L24.66 7.34 L28.12 9.00 L27.83 12.83 L30.00 16.00 L27.83 19.17 L28.12 23.00 L24.66 24.66 L23.00 28.12 L19.17 27.83 L16.00 30.00 L12.83 27.83 L9.00 28.12 L7.34 24.66 L3.88 23.00 L4.17 19.17 L2.00 16.00 L4.17 12.83 L3.88 9.00 L7.34 7.34 L9.00 3.88 L12.83 4.17Z",
  check: "M11.75 16.25 14.75 19.25 20.5 13.25",
};

export function LogoGlyph() {
  return (
    <>
      <path d={GLYPH.rosette} fill="var(--logo-seal)" />
      <circle
        cx={16}
        cy={16}
        r={9.25}
        fill="none"
        stroke="var(--logo-ring)"
        strokeWidth={1.1}
      />
      <path
        d={GLYPH.check}
        fill="none"
        stroke="var(--logo-check)"
        strokeWidth={2.5}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </>
  );
}
