from typing import Optional
from fastapi import Request, HTTPException
from fastapi.responses import RedirectResponse


def require_user(request: Request) -> dict:
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    return user


class RedirectIfNotAuthenticated:
    """Dependency that redirects to /login instead of raising 401."""

    def __call__(self, request: Request) -> dict:
        user = request.session.get("user")
        if not user:
            # For page routes we want a redirect, not a JSON 401
            from fastapi.responses import RedirectResponse
            raise HTTPException(
                status_code=307,
                headers={"Location": "/login"},
            )
        return user
