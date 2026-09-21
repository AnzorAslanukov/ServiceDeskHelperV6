"""
FastAPI application entry point for the Service Desk Helper.
"""

from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from src.dependencies import get_athena_client, get_current_user, get_databricks_client
from src.routers import assignment, auth, bug_report, chat, search, turnover
from src.routers import frontend as frontend_router
from feature4.router import router as bulk_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle — startup and shutdown."""
    # Startup: nothing special needed (clients lazy-init)
    yield
    # Shutdown: close client connections
    athena = get_athena_client()
    databricks = get_databricks_client()
    await athena.close()
    await databricks.close()


app = FastAPI(
    title="Service Desk Helper",
    description="AI-powered IT service desk assistant for Penn Medicine. "
    "Provides enhanced ticket search, semantic similarity, and knowledge base retrieval.",
    version="0.1.0",
    lifespan=lifespan,
)


# ── Exception Handlers ─────────────────────────────────────────────────
# Translate upstream/validation failures into clean, structured responses so
# they never surface as unhandled 500s (e.g. Athena HTTP 500 on a bad filter,
# or a ValueError from invalid/empty search input).


@app.exception_handler(httpx.HTTPStatusError)
async def athena_upstream_error_handler(request: Request, exc: httpx.HTTPStatusError):
    """An error from an upstream API (Athena/Databricks) maps to 502 Bad Gateway."""
    status = exc.response.status_code if exc.response is not None else "unknown"
    return JSONResponse(
        status_code=502,
        content={"detail": f"Upstream service error (HTTP {status})."},
    )


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
    """A ValueError from the service/client layer maps to 400 Bad Request."""
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# ── Authentication Middleware ──────────────────────────────────────────

# Paths that don't require authentication
PUBLIC_PATHS = {"/login", "/logout", "/health", "/docs", "/openapi.json", "/redoc"}
PUBLIC_PREFIXES = ("/static/", "/bulk-static/")


class AuthMiddleware(BaseHTTPMiddleware):
    """Redirect unauthenticated users to /login for protected routes."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Always initialize user state (templates depend on this)
        request.state.user = None

        # Allow public paths
        if path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES):
            return await call_next(request)

        # Check session
        user = get_current_user(request)
        if user is None:
            return RedirectResponse(url="/login", status_code=302)

        # Attach user to request state for templates
        request.state.user = user
        return await call_next(request)


app.add_middleware(AuthMiddleware)

# Mount static files for the frontend
STATIC_DIR = Path(__file__).resolve().parent.parent / "frontend" / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Mount Feature #4 static files (isolated from core frontend)
BULK_STATIC_DIR = Path(__file__).resolve().parent.parent / "feature4" / "static"
app.mount("/bulk-static", StaticFiles(directory=str(BULK_STATIC_DIR)), name="bulk-static")

# Register auth router (login/logout — must be first)
app.include_router(auth.router)

# Register API routers
app.include_router(search.router)
app.include_router(chat.router)
app.include_router(assignment.router)
app.include_router(turnover.router)
app.include_router(bug_report.router)
app.include_router(bulk_router)

# Register frontend router
app.include_router(frontend_router.router)


@app.get("/", include_in_schema=False)
async def root_redirect():
    """Redirect root URL to the frontend search page."""
    return RedirectResponse(url="/ui/search", status_code=302)


@app.get("/health", tags=["system"])
async def health_check():
    """Health check endpoint."""
    return {"status": "ok"}
