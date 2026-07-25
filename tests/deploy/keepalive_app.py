"""Tiny integration-only ASGI app for the Caddy/Uvicorn keep-alive contract."""

from fastapi import FastAPI

app = FastAPI()


@app.post("/search")
async def search() -> dict[str, bool]:
    return {"ok": True}
