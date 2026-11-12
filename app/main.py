import logging
import time

from fastapi import FastAPI, HTTPException, Request, status
from sqlalchemy import text

from app.api.routes_files import router as files_router
from app.core.logging import setup_logging
from app.db.session import session_scope

logger = logging.getLogger("docforge.api")


def create_app() -> FastAPI:
    setup_logging()
    app = FastAPI(
        title="Docforge",
        version="0.1.0",
    )

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        logger.info(
            "request",
            extra={
                "fields": {
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                }
            },
        )
        return response

    @app.get("/health")
    def health() -> dict[str, str]:
        try:
            with session_scope() as db:
                db.execute(text("SELECT 1"))
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="database unavailable") from exc
        return {"status": "ok"}

    app.include_router(files_router)
    return app

app = create_app()