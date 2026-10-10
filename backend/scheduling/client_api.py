"""Minimal client route, scoped entirely by a verified server-side identity link."""

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from scheduling.identity_links import IdentityLink, LinkRole


class ClientSession(BaseModel):
    role: str = "client"
    business_id: str
    client_id: str


def add_client_session_route(
    app: FastAPI, verify_token: Callable[[str], IdentityLink]
) -> None:
    """Expose only the caller's linked identifiers; no browser-supplied scope."""
    security = HTTPBearer(auto_error=False)

    @app.get("/v1/client/session", response_model=ClientSession)
    def client_session(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
    ) -> ClientSession:
        if credentials is None:
            raise HTTPException(status_code=401, detail="Client authentication is required")
        try:
            link = verify_token(credentials.credentials)
        except Exception as exc:
            raise HTTPException(status_code=401, detail="Invalid client credentials") from exc
        if link.role != LinkRole.CLIENT or not link.client_id:
            raise HTTPException(status_code=403, detail="Client access is required")
        return ClientSession(business_id=link.business_id, client_id=link.client_id)
