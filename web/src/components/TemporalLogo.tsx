// Temporal brand mark.
//
// TemporalMark renders the official Temporal symbol from web/public: the "light"
// variant (near-white) for dark backgrounds, the "dark" variant (black) for light
// ones. TemporalLogo is the top-bar lockup — the light symbol plus the wordmark.
// The Temporal name, wordmark, and symbol are trademarks of Temporal Technologies;
// use them per https://temporal.io/brand.

interface MarkProps {
  size?: number;
  className?: string;
  variant?: "light" | "dark";
}

export function TemporalMark({
  size = 28,
  className,
  variant = "dark",
}: MarkProps) {
  return (
    <img
      className={className}
      src={`/temporal-symbol-${variant}.png`}
      width={size}
      height={size}
      alt=""
      aria-hidden="true"
    />
  );
}

interface LogoProps {
  size?: number;
  withWordmark?: boolean;
  tone?: "light" | "dark"; // wordmark color: light = on dark bg, dark = on light bg
}

export function TemporalLogo({
  size = 26,
  withWordmark = true,
  tone = "light",
}: LogoProps) {
  const wordColor = tone === "light" ? "var(--off-white)" : "var(--space-black)";
  return (
    <span className="logo" aria-label="Temporal">
      <TemporalMark className="logo__mark" size={size} variant="light" />
      {withWordmark && (
        <span className="logo__wordmark" style={{ color: wordColor }}>
          temporal
        </span>
      )}
    </span>
  );
}
