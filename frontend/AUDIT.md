# Frontend maintainability audit — 2026-07-22

Two remediation passes were completed after the account-center and Motion implementation.

## Round 1 — duplicated sources and visual drift

Findings:

- raw colors and an undefined font token were spread through the application stylesheet;
- route strings, API base handling, product limits, query keys, and Motion timings were repeated;
- handwritten animation names existed beside Motion presets;
- locale-specific `Intl` calls were embedded in pages;
- no explicit product/design context prevented future visual drift.

Remediation:

- added `PRODUCT.md`, `DESIGN.md`, semantic theme tokens, and an automated architecture check;
- centralized routes, product/runtime config, query keys, private prefetch, and Motion presets;
- removed CSS keyframes and kept simple hover/focus transitions in CSS;
- introduced typed locale/theme providers and centralized date/time formatting;
- added tests for route, locale, theme, reduced-motion, skeleton, and account-center behavior.

## Round 2 — module boundaries and latent defects

Findings:

- a 2,390-line CSS Module mixed every page and contained obsolete account/quota rules;
- Access Keys and Account pages combined orchestration with complex overlays;
- two class names were referenced without definitions and silently evaluated to `undefined`;
- cold-loaded search results could remain behind an exiting Skeleton with Presence `wait` mode.

Remediation:

- split styling into ordered responsibility layers, removed obsolete rules, and added 900-line and
  undefined-class budgets;
- moved Access Key and account deletion overlays into feature modules and added a 400-line UI
  module budget;
- changed search state Presence to `popLayout`, preserving the quiet fade while removing the
  cold-load race;
- reran desktop/mobile layout, custom Select, reduced-motion, search, account-menu, usage, history,
  and one-time-secret browser flows.

## Current assessment

- Accessibility: semantic controls, focus restoration, reduced motion, hidden chart tables, and
  axe coverage are present.
- Extensibility: routes, API, copy/locale, theme tokens, query keys, and Motion each have one owner.
- Visual consistency: Figma-aligned geometry remains covered by screenshots and exact layout tests.
- Known optimization: the public entry chunk still triggers Vite's 500 kB warning. Private account
  pages and Motion features are already lazy; further vendor chunking is a performance optimization,
  not a correctness or maintainability blocker.
