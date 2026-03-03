"""
JWT authentication helpers for API request authorization.
"""

from __future__ import annotations

from typing import Any

import jwt
from jwt import InvalidTokenError

from aci.config import AuthConfig, PlatformConfig

PUBLIC_PATH_PREFIXES = ("/platform",)
PUBLIC_PATHS = {"/", "/health", "/live", "/ready", "/metrics"}


def is_public_path(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in PUBLIC_PATH_PREFIXES)


def is_auth_required(path: str) -> bool:
    return path.startswith("/v1/")


def can_bypass_auth(config: PlatformConfig) -> bool:
    return config.environment.lower() in {"development", "dev", "test", "testing", "local"} and (
        config.auth.allow_dev_bypass
    )


def decode_and_validate_token(
    token: str,
    config: AuthConfig,
    expected_tenant: str,
) -> dict[str, Any]:
    """Decode and validate JWT claims for service-to-service authentication."""
    key = _jwt_key(config)
    claims = jwt.decode(
        token,
        key=key,
        algorithms=[config.jwt_algorithm],
        audience=config.jwt_audience,
        issuer=config.jwt_issuer,
        leeway=config.clock_skew_seconds,
        options={
            "require": ["iss", "aud", "exp", "iat", "sub"],
        },
    )

    tenant_claim = config.tenant_claim
    if claims.get(tenant_claim) != expected_tenant:
        raise InvalidTokenError(
            f"token tenant mismatch: claim '{tenant_claim}' does not match expected tenant"
        )

    required_scope = config.required_scope.strip()
    if required_scope:
        scopes = _normalize_scopes(claims.get("scope"))
        if required_scope not in scopes:
            raise InvalidTokenError(f"required scope '{required_scope}' is missing")

    return claims


def _jwt_key(config: AuthConfig) -> str:
    algorithm = config.jwt_algorithm.upper()
    if algorithm.startswith("HS"):
        return config.jwt_hs256_secret
    return config.jwt_public_key_pem


def _normalize_scopes(raw_scope: Any) -> set[str]:
    if raw_scope is None:
        return set()
    if isinstance(raw_scope, str):
        return {item for item in raw_scope.strip().split() if item}
    if isinstance(raw_scope, list):
        return {str(item) for item in raw_scope}
    return {str(raw_scope)}
