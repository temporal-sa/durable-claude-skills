# Brand assets

The official Temporal symbol ships in two variants:

- `temporal-symbol-light.png` (near-white) for dark backgrounds — the favicon
  (`web/index.html`) and the top-bar brand mark.
- `temporal-symbol-dark.png` (black) for light backgrounds — the assistant
  avatars, the empty-state hero, and the confirmation-card header.

Both are rendered by the `TemporalMark` component in
`src/components/TemporalLogo.tsx` (`variant="light" | "dark"`); `TemporalLogo` is
the top-bar lockup (light symbol + wordmark).

The Temporal name, wordmark, and symbol are trademarks of Temporal Technologies;
use them per the brand guidelines at https://temporal.io/brand.
