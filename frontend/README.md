# Scholight frontend

Vite, React, and TypeScript client for Scholight. Browser requests always use the relative `/api` base. During local development Vite runs at `127.0.0.1:7200`, forwards that prefix to `127.0.0.1:7201`, and removes `/api` before the request reaches FastAPI.

```bash
npm install
npm run api:generate
npm run dev
```

Run the complete frontend quality gate with `npm run verify`. The generated OpenAPI schema and TypeScript declarations are committed; `npm run api:check` fails if the current backend contract has drifted.

The production Docker image builds the static app and serves it as a non-root nginx process on port `8080`. nginx proxies `/api/` to the Compose service named `api`, exposes `/healthz` for container liveness, and falls back to `index.html` for client routes.
