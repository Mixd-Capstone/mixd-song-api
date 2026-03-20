import os
from pathlib import Path

from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

from app.config import RATE_LIMIT
from app.routes import health, search, download, songs, admin, cache

limiter = Limiter(key_func=get_remote_address, default_limits=[RATE_LIMIT])

app = FastAPI(title="Mixd Song Ingestion API", version="1.0.0")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(health.router, prefix="/api")
app.include_router(search.router, prefix="/api")
app.include_router(download.router, prefix="/api")
app.include_router(songs.router, prefix="/api")
app.include_router(admin.router, prefix="/api")
app.include_router(cache.router, prefix="/api")

# Serve Web UI (SPA — all non-API routes serve index.html)
_webui_dir = Path(__file__).resolve().parent.parent / "webui"

if _webui_dir.is_dir():
    app.mount("/webui", StaticFiles(directory=str(_webui_dir)), name="webui")

@app.get("/{full_path:path}")
async def serve_ui(full_path: str = ""):
    return FileResponse(_webui_dir / "index.html")
