"""Shared auth dependencies for the control plane."""

from __future__ import annotations

from fastapi import Header, HTTPException, Request

from ..config import get_settings
from ..security import extract_admin_token, is_loopback_host


def require_control_plane_access(
    request: Request,
    authorization: str | None = Header(default=None),
    x_edict_admin_token: str | None = Header(default=None),
):
    settings = get_settings()
    expected = settings.admin_api_token.strip()
    provided = extract_admin_token(authorization, x_edict_admin_token)
    client_host = request.client.host if request.client else ""

    if expected:
        if provided != expected:
            raise HTTPException(status_code=403, detail="admin token required")
        return

    if not is_loopback_host(client_host):
        raise HTTPException(
            status_code=403,
            detail="remote control-plane access requires EDICT_ADMIN_API_TOKEN",
        )
