"""
Clerk JWT verification.
Frontend sends Clerk session token in Authorization header.
Backend verifies it using Clerk's JWKS endpoint.
No redirect flow needed — Clerk handles all UI on frontend.
"""
import os
import httpx
import jwt as pyjwt
from fastapi import HTTPException, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

CLERK_SECRET_KEY = os.environ.get("CLERK_SECRET_KEY", "")
CLERK_PUBLISHABLE_KEY = os.environ.get("CLERK_PUBLISHABLE_KEY", "")

# Extract frontend API from publishable key
# pk_live_xxx → extract instance FAPI
def _get_frontend_api() -> str:
    pk = CLERK_PUBLISHABLE_KEY
    if not pk:
        return ""
    try:
        import base64
        # Clerk publishable key format: pk_live_BASE64
        parts = pk.split("_")
        if len(parts) >= 3:
            decoded = base64.b64decode(parts[2] + "==").decode()
            return decoded.rstrip("$")
    except Exception:
        pass
    return ""

_bearer = HTTPBearer(auto_error=False)
_jwks_cache = {}


async def _get_jwks():
    fapi = _get_frontend_api()
    if not fapi:
        return {}
    if fapi in _jwks_cache:
        return _jwks_cache[fapi]
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"https://{fapi}/.well-known/jwks.json")
            data = r.json()
            _jwks_cache[fapi] = data
            return data
    except Exception:
        return {}


async def verify_clerk_token(
    credentials: HTTPAuthorizationCredentials = Security(_bearer),
) -> dict:
    """
    Dependency: inject into any route that needs authentication.
    Returns decoded token payload with user info.
    """
    if not credentials:
        raise HTTPException(401, "Not authenticated")

    token = credentials.credentials

    try:
        # Decode without verification first to get kid
        header = pyjwt.get_unverified_header(token)
        kid = header.get("kid")

        jwks = await _get_jwks()
        keys = jwks.get("keys", [])
        public_key = None

        for key_data in keys:
            if key_data.get("kid") == kid:
                public_key = pyjwt.algorithms.RSAAlgorithm.from_jwk(key_data)
                break

        if not public_key:
            raise HTTPException(401, "Invalid token: key not found")

        payload = pyjwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            options={"verify_aud": False},
        )
        return payload

    except pyjwt.ExpiredSignatureError:
        raise HTTPException(401, "Token expired")
    except pyjwt.InvalidTokenError as e:
        raise HTTPException(401, f"Invalid token: {e}")


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Security(_bearer),
) -> dict:
    """
    Returns user dict from our user_store.
    Creates user on first login automatically.
    """
    payload = await verify_clerk_token(credentials)

    clerk_user_id = payload.get("sub", "")
    email = payload.get("email", "")
    name = f"{payload.get('first_name', '')} {payload.get('last_name', '')}".strip()

    import user_store
    user = user_store.get_or_create(clerk_user_id, email=email, name=name)
    user["clerk_user_id"] = clerk_user_id
    return user


async def get_optional_user(
    credentials: HTTPAuthorizationCredentials = Security(_bearer),
) -> dict | None:
    """Returns user if logged in, None if not. For optional auth routes."""
    if not credentials:
        return None
    try:
        return await get_current_user(credentials)
    except HTTPException:
        return None
