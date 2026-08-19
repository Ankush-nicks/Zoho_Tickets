import secrets

from fastapi import HTTPException, Request

from . import config


def verify_credentials(username: str, password: str) -> bool:
    return secrets.compare_digest(username, config.ADMIN_USERNAME) and secrets.compare_digest(
        password, config.ADMIN_PASSWORD
    )


def require_login(request: Request) -> str:
    """Dependency for API routes: 401s if the session isn't logged in."""
    user = request.session.get("user")
    if not user:
        raise HTTPException(401, "Not logged in.")
    return user
