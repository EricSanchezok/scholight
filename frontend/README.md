# Scholight frontend

Vite, React, and TypeScript client for Scholight. Browser requests always use the relative `/api` base. During local development Vite runs at `127.0.0.1:7200`, forwards that prefix to `127.0.0.1:7201`, and removes `/api` before the request reaches FastAPI.

```bash
npm install
npm run api:generate
npm run dev
```

Run the complete frontend quality gate with `npm run verify`. The generated OpenAPI schema and TypeScript declarations are committed; `npm run api:check` fails if the current backend contract has drifted.

## Brand assets

Scholight has one canonical lynx artwork at `brand/source/scholight-lynx-master.png`.
The outer corners of that square source are transparent; only the circular portrait
and lynx artwork remain visible when it is placed on a page. Web product-mark exports
preserve that unframed portrait. Browser, PWA, and installation icons place a scaled
copy of the same portrait inside one near-black circular launcher badge so the warm
disc remains legible on a light operating-system tile. No CSS border or separately
drawn small-size artwork is permitted.

All browser, PWA, social, UI, and native-handoff files are deterministic derivatives
of the master. After an intentional master change, run `npm run brand:build` and commit
the source and generated outputs together. `npm run brand:check` verifies the recorded
source hash, dimensions, launcher ring, transparent standard-icon corners, and maskable
opacity. The brand check is included in `npm run verify`.

The production Docker image builds the static app and serves it as a non-root nginx process on port `8080`. nginx proxies `/api/` to the Compose service named `api`, exposes `/healthz` for container liveness, and falls back to `index.html` for client routes.
