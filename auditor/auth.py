from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import jwt
from jwt import InvalidTokenError

from auditor.persistence import audit_auth_event
from auditor.settings import get_settings


@dataclass(slots=True)
class AuthContext:
    subject: str
    roles: set[str]
    raw_claims: dict[str, Any]


def verify_jwt_token(token: str) -> AuthContext:
    settings = get_settings()
    claims = jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=[settings.jwt_algorithm],
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
        options={"require": ["sub", "iss", "aud", "exp"]},
    )
    roles_raw = claims.get("roles", [])
    roles = set(roles_raw) if isinstance(roles_raw, list) else set()
    subject = str(claims.get("sub", "unknown"))
    return AuthContext(subject=subject, roles=roles, raw_claims=claims)


def enforce_role(token: str, required_role: str, action: str) -> AuthContext:
    try:
        context = verify_jwt_token(token)
    except InvalidTokenError as exc:
        audit_auth_event("unknown", action, "denied", f"invalid_token: {exc}")
        raise PermissionError("Invalid JWT token") from exc

    if required_role not in context.roles:
        audit_auth_event(context.subject, action, "denied", f"missing_role:{required_role}")
        raise PermissionError(f"Missing required role: {required_role}")
    audit_auth_event(context.subject, action, "allowed", f"role:{required_role}")
    return context
