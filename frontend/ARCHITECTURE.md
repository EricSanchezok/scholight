# Frontend architecture

Scholight's frontend is organized around explicit sources of truth. New features should extend
these registries instead of introducing local constants or parallel conventions.

## Dependency direction

```text
pages -> features -> components -> app/config/i18n/theme -> api/lib
```

- Pages own route-level data orchestration and compose feature components.
- Features own domain interactions such as Access Key overlays and account deletion.
- Components are reusable interaction primitives and do not construct API paths.
- API domain modules are the only place that call the generated client.
- `src/api/schema.d.ts` is generated from the backend OpenAPI document and is never hand edited.

## Sources of truth

| Concern          | Source                        | Extension rule                                                              |
| ---------------- | ----------------------------- | --------------------------------------------------------------------------- |
| Routes           | `src/app/routes.ts`           | Add the path and use its exported descriptor everywhere.                    |
| Query keys       | `src/app/queryKeys.ts`        | Add keys under the public or `privateRoot` hierarchy.                       |
| Private prefetch | `src/app/privateRoutes.ts`    | Register lazy code and data prefetch together.                              |
| Product limits   | `src/config/product.ts`       | Keep user-facing product constants out of pages.                            |
| API base         | `src/config/runtime.ts`       | Browser requests remain relative to `/api`.                                 |
| Motion           | `src/app/motion.tsx`          | Reuse a named preset; timings only live here.                               |
| Copy and locale  | `src/i18n/en.ts`, `src/i18n/` | Add catalogs with the same typed shape and format through `i18n/format.ts`. |
| Theme            | `src/theme/ThemeProvider.tsx` | Register a theme and provide a complete token scope before exposing it.     |
| Visual tokens    | `src/styles/tokens.css`       | Components consume semantic roles, never raw colors.                        |
| Style order      | `src/styles/app.css`          | Add a responsibility layer and import it once from `main.tsx`.              |
| Style classes    | `src/styles/classes.ts`       | Identity registry for the global Scholight style layers.                    |

## Styling layers

The stylesheet order is intentional:

1. `tokens.css` defines theme-scoped semantic roles.
2. `app.css` composes shell, search, content, account center, state, and responsive layers.
3. `global.css` applies accessibility and element-level defaults last, so focus-visible styles
   cannot be accidentally reset by a component rule.

No stylesheet may exceed 900 lines. Runtime modules do not import CSS directly; this keeps
cascade order deterministic. `check_architecture.mjs` verifies tokens, class references, raw
colors, animation declarations, route literals, locale formatting, and file-size budgets.

## Motion policy

Motion communicates state and continuity. The system uses low-amplitude opacity/position changes,
short ease-out timing, and asynchronous Motion features. It does not use bounce, parallax, number
rolling, or decorative loops. The only repeating motion is indeterminate loading feedback, and it
becomes static under reduced motion.

CSS owns hover, focus, color, and border transitions. Motion owns Presence, route transitions,
stagger, skeleton feedback, and Radix surface entry/exit.

## Adding a locale or theme

To add a locale, create a catalog with the `Messages` shape, register it in `I18nProvider`, and
pass the active locale to the centralized formatters. High-reuse navigation and search copy is
already catalog-backed; feature copy should move into the catalog as that feature is translated.

To add a theme, define every semantic token under a new `[data-theme="..."]` scope, register the
name in `ThemeProvider`, and visually verify all route and dialog screenshots. Components must not
branch on a theme name.
