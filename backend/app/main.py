"""FastAPI application entrypoint.

Routers for news/rewrites are mounted in the API section; for now the app
exposes health/meta endpoints so the container, the compose healthcheck and the
frontend all have something real to talk to.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.core.database import get_db_path, healthcheck

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("moodnews")

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    logger.info("Starting %s", settings.app_name)
    logger.info("SQLite database: %s", get_db_path())
    if not settings.llm_configured:
        logger.warning(
            "LLM_API_KEY is not set - mood rewriting will be unavailable "
            "until it is configured (see .env.example)."
        )
    yield
    logger.info("Shutting down %s", settings.app_name)


app = FastAPI(
    lifespan=lifespan,
    title=settings.app_name,
    description=(
        "Read real news rewritten in different emotional tones, "
        "with every fact preserved and verified."
    ),
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", tags=["meta"])
def health() -> dict:
    """Liveness + database reachability, used by the compose healthcheck."""
    db_ok = healthcheck()
    return {
        "status": "ok" if db_ok else "degraded",
        "database": "ok" if db_ok else "unreachable",
        "llm_configured": settings.llm_configured,
    }


@app.get("/", tags=["meta"])
def root() -> dict:
    return {
        "name": settings.app_name,
        "version": app.version,
        "docs": "/docs",
    }
