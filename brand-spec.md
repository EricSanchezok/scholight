# Scholight Product Icon — Brand Spec

## Scope

This spec governs the approved Scholight product icon and every derivative used by
the web, PWA, browser chrome, social previews, and native handoff. Scholight is the
academic research engine in the SanchezCloud product family. The icon is a sibling
of the existing Synergy and Scholens mascots while remaining a distinct Scholight
mark.

## Reference assets

- Synergy product icon: `/Users/eric/projects/synergy/packages/ui/src/assets/brand/synergy-product-icon.png`
  — a centered panda portrait with dark circular badge, ivory face, black scarf,
  and dark eyewear.
- Scholens product icon: `/Users/eric/projects/scholens/web/public/brand/icons/icon-192.png`
  — a centered raven portrait with dark circular badge, ivory disc, black plumage,
  and a precise silver monocle.
- Scholight product design system: `DESIGN.md` in this repository.

## Approved Scholight mark

Scholight uses a large, anthropomorphic lynx in a calm three-quarter left-facing
portrait. The ear tufts, attentive eyes, cheek planes, and warm ivory muzzle are the
recognition points. The lynx represents acute observation and the act of finding
connections in unfamiliar literature.

The approved master is:

- `frontend/brand/source/scholight-lynx-master.png`
- 720 × 720 px, indexed RGBA PNG with transparent pixels outside the circular badge
- SHA-256: `996d7777c6aa35932227196cb56b9ca32c0cc3b2be5a6c321d3b6001f3fc0579`

The master is the only authored artwork. Small sizes are deterministic resizes of
this file; if a size loses legibility, revise the master once and regenerate every
derivative. Never draw a separate small-size expression, pose, crop, or silhouette.

## Shared family language

- Square raster artwork with a strong circular badge and a silhouette that reads
  at small sizes. The area outside the circular badge is transparent so the same
  mark can sit cleanly on the warm canvas at any responsive size.
- Quiet near-black, charcoal, ivory, and soft-gray base palette.
- Broad tonal planes and a restrained editorial illustration; avoid generic flat
  vector clip-art, fine fur texture, and noisy photorealism.
- Calm, observant, intelligent mood. The mascot may be personable, but should
  not become childish or comedic.

## Scholight-specific expression

The first icon exploration is intentionally achromatic, following the authored
Scholens asset rules and the restrained Synergy reference: near-black, charcoal,
ivory, and soft gray only. Scholight's interface may continue to use its semantic
blue (`#1F45B8`) for actions and selection, but the mascot artwork should not add
blue, gold, or other colored accents. The animal itself is the product signature;
light is expressed through contrast and a quiet ivory focal area rather than a
neon beam.

Scholight uses a third animal distinct from both the Synergy panda and the Scholens
raven. Its three-quarter left-facing pose avoids Scholens's right-facing
side-profile lens grammar and avoids Synergy's sunglasses/scarf silhouette. The
lynx has no glasses, monocle, sunglasses, scarf, collar, letterform, or optical
device.

## Prohibited treatments

- No wordmark, letters, initials, slogans, watermark, or UI chrome inside the icon.
- No rainbow palette, neon aura, dominant gradient, glossy 3D treatment, or generic
  AI-tech glow.
- No duplicate Scholens lettermark or attempt to reuse the Synergy SII arrow as
  the product icon.
- No tiny details that disappear below approximately 32px. At 16px the silhouette
  and ivory face must remain distinguishable.

## Generated asset pipeline

Run the deterministic generator from `frontend/`:

```bash
npm run brand:build
npm run brand:check
```

It validates the master hash and dimensions, then generates favicon ICO entries
(16/32/48), Apple touch icon (180), web/PWA icons (192/512), an opaque maskable
512px icon, 64/128 UI portraits, a 1200 × 630 social image, and 64/128/256/512/1024
native handoff files. Generated output must not be edited directly.

The standard site header keeps the Scholight wordmark as its primary identity. The
lynx is used in the desktop home hero, authentication, documentation lead-in,
empty/error states, PWA/browser chrome, and social previews; it is not repeated
beside every paper result or account row. Below 768px the home hero prioritizes the
search task and hides the artwork instead of introducing a separate mobile crop.
