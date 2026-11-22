"""Browser recovery entry and mandatory password-change request gate."""
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from identity import session_token_from_request, uses_admin_session

router = APIRouter()
_RECOVERY_PATHS = {
    "/change-password", "/login", "/admin/login",
    "/api/auth/login", "/api/auth/admin-login",
    "/api/auth/me", "/api/auth/password", "/api/auth/logout",
}


@router.get("/change-password", response_class=HTMLResponse, include_in_schema=False)
def change_password_page():
    """Return a noncached recovery form; the password API authenticates submission."""
    page = Path(__file__).parent / "templates" / "change_password.html"
    return HTMLResponse(page.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})


def recovery_response(request: Request, memory):
    """Block business access for sessions awaiting a mandatory password change.

    Args: request: Incoming HTTP request; memory: Session repository.
    Returns: Redirect/403 for restricted sessions, otherwise None.
    Raises: Repository errors propagate so recovery fails closed.
    Example: response = recovery_response(request, memory).
    """
    if request.url.path in _RECOVERY_PATHS or request.url.path.startswith("/static/"):
        return None
    credential = session_token_from_request(request)
    if not credential:
        return None
    token = credential[7:] if credential.lower().startswith("bearer ") else credential
    session = memory.get_auth_session(token)
    if not session or not session.get("password_change_required"):
        return None
    target = "/change-password" + ("?admin=1" if uses_admin_session(request) else "")
    if request.method in {"GET", "HEAD"} and "text/html" in request.headers.get("accept", ""):
        return RedirectResponse(target, status_code=303)
    return JSONResponse(
        {"detail": "请先修改临时密码", "code": "PASSWORD_CHANGE_REQUIRED", "redirect": target},
        status_code=403,
    )
