"""
FastAPI application entry point for the Service Desk Helper.
"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from src.dependencies import get_athena_client, get_current_user, get_databricks_client
from src.routers import assignment, auth, chat, search, turnover
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
