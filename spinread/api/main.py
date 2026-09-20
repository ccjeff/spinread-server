"""FastAPI app: routers, CORS, error envelope, startup bootstrap + embed worker."""

from __future__ import annotations

import logging
import threading

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from spinread.api import deps
from spinread.api.errors import ApiError, api_error_handler, error_body
from spinread.api.routers import auth, media, uploads, videos
from spinread.config import get_settings
from spinread.core.seed import seed_demo_user

log = logging.getLogger(__name__)

app = FastAPI(title="SpinRead MLP API", version="0.1.0")

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_exception_handler(ApiError, api_error_handler)


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500, content=error_body("INTERNAL_ERROR", "internal server error")
    )


app.include_router(auth.router)
app.include_router(uploads.router)
app.include_router(videos.router)
app.include_router(media.router)


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.on_event("startup")
def startup() -> None:
    logging.basicConfig(level=logging.INFO)
    deps.init_singletons(settings)
    deps.get_s3().bootstrap()

    session_factory = deps._session_factory
    session = session_factory()
    try:
        seed_demo_user(session)
        session.commit()
    finally:
        session.close()

    if settings.embed_worker:
        from spinread.worker.main import run_worker

        t = threading.Thread(
            target=run_worker,
            kwargs={"settings": settings, "worker_id": "embed-worker"},
            daemon=True,
            name="spinread-embed-worker",
        )
        t.start()
        log.info("embedded worker thread started")
