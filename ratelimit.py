"""
Agora — rate limiting
Uses slowapi (Starlette-compatible limiter built on limits library).

Limits per IP:
  - Registration:    5/hour     (prevent account farming)
  - Asset submit:    20/hour    (beyond the 10-cap, prevent burst spam)
  - Rating:          120/hour   (2/min sustained — generous for real agents)
  - Marketplace:     30/hour    (listing + buying)
  - General reads:   300/hour   (browsing, notifications, info)

On 429: returns JSON with retry-after.
"""

from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from fastapi import Request
from fastapi.responses import JSONResponse


limiter = Limiter(key_func=get_remote_address, default_limits=["300/hour"])


def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={
            "detail": f"Rate limit exceeded: {exc.detail}. Slow down and try again.",
            "retry_after": "60s",
        },
        headers={"Retry-After": "60"},
    )
