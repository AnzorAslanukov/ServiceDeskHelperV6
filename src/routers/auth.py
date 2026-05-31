"""
Authentication Router — Login/Logout pages and session management.
"""

import logging

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from src.dependencies import get_auth_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "frontend" / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

SESSION_COOKIE = "sdh_session"


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    """Render the login page."""
    return templates.TemplateResponse(
        request, "auth/login.html", {"error": error}
    )


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    """Handle login form submission."""
    auth_service = get_auth_service()

    try:
        user = auth_service.authenticate(username, password)
    except ValueError as e:
        # Authorization failure (valid creds but wrong group)
        return templates.TemplateResponse(
            request, "auth/login.html", {"error": str(e)}, status_code=403
        )

    if user is None:
        # Invalid credentials
        return templates.TemplateResponse(
            request, "auth/login.html",
            {"error": "Invalid username or password."},
            status_code=401,
        )

    # Create session token and set cookie
    token = auth_service.create_session_token(user)
    response = RedirectResponse(url="/ui/search", status_code=302)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=int(auth_service.session_expire_seconds),
        path="/",
    )
    logger.info("User %s logged in successfully", user.username)
    return response


@router.get("/logout")
async def logout(response: Response):
    """Clear session and redirect to login."""
    resp = RedirectResponse(url="/login", status_code=302)
    resp.delete_cookie(key=SESSION_COOKIE, path="/")
    return resp