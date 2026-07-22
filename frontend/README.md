# Scholight frontend

Vite, React, and TypeScript client for Scholight. Browser requests always use the relative `/api` base. During local development Vite forwards that prefix to `localhost:8000` and removes `/api` before the request reaches FastAPI.

```bash
npm install
npm run api:generate
npm run dev
```

Run the complete frontend quality gate with `npm run verify`. The generated OpenAPI schema and TypeScript declarations are committed; `npm run api:check` fails if the current backend contract has drifted.

The production Docker image builds the static app and serves it through nginx. nginx proxies `/api/` to the Compose service named `api` and falls back to `index.html` for client routes.
