"""OAuth2 token handling: refresh before expiry and persist the rotated refresh token.

Miele's Keycloak issues short-lived access tokens (one hour observed) against a
long-lived refresh token, and rotates the refresh token on every exchange. The
initial refresh token comes from the SealedSecret; every rotation afterwards is
written to a PVC, because writing back into an ArgoCD-managed Secret would
conflict with GitOps ownership.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from .config import Settings
from .metrics import Metrics

logger = logging.getLogger(__name__)

_REFRESH_MAX_ATTEMPTS = 5
_REFRESH_BACKOFF_INITIAL = 5.0
_REFRESH_BACKOFF_MAX = 120.0


class ConsentRequiredError(RuntimeError):
    """The refresh token is no longer accepted; a new consent round is required.

    Retrying cannot fix this, so it is kept distinct from transient HTTP errors.
    """


class TokenManager:
    def __init__(self, settings: Settings, metrics: Metrics, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._metrics = metrics
        self._client = client
        self._client_id = settings.read_client_id()
        self._client_secret = settings.read_client_secret()
        self._refresh_token = settings.read_refresh_token()
        self._access_token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def access_token(self) -> str:
        """Return a valid access token, refreshing it when inside the margin."""
        async with self._lock:
            margin = self._settings.token_refresh_margin_seconds
            if self._access_token and time.time() < self._expires_at - margin:
                return self._access_token
            await self._refresh()
            assert self._access_token is not None
            return self._access_token

    async def _refresh(self) -> None:
        backoff = _REFRESH_BACKOFF_INITIAL
        for attempt in range(1, _REFRESH_MAX_ATTEMPTS + 1):
            try:
                response = await self._client.post(
                    self._settings.token_url,
                    data={
                        "grant_type": "refresh_token",
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "refresh_token": self._refresh_token,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
            except httpx.HTTPError as exc:
                self._metrics.api_errors.labels(reason="token_transport").inc()
                logger.warning("token refresh transport error (attempt %d): %s", attempt, exc)
            else:
                if response.status_code == httpx.codes.OK:
                    self._store(response.json())
                    return
                if response.status_code in (
                    httpx.codes.BAD_REQUEST,
                    httpx.codes.UNAUTHORIZED,
                ):
                    # invalid_grant means the refresh token was revoked, expired or
                    # already superseded by a rotation we failed to persist.
                    self._metrics.api_errors.labels(reason="token_rejected").inc()
                    raise ConsentRequiredError(
                        f"refresh token rejected ({response.status_code}): {response.text[:200]}"
                    )
                self._metrics.api_errors.labels(reason="token_http").inc()
                logger.warning(
                    "token refresh failed with HTTP %d (attempt %d)",
                    response.status_code,
                    attempt,
                )

            if attempt < _REFRESH_MAX_ATTEMPTS:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _REFRESH_BACKOFF_MAX)

        raise RuntimeError(f"token refresh failed after {_REFRESH_MAX_ATTEMPTS} attempts")

    def _store(self, payload: dict[str, object]) -> None:
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise RuntimeError("token response contained no access_token")

        expires_in = payload.get("expires_in")
        lifetime = float(expires_in) if isinstance(expires_in, int | float) else 3600.0
        self._access_token = access_token
        self._expires_at = time.time() + lifetime
        self._metrics.token_expiry.set(self._expires_at)

        # Keycloak rotates the refresh token on every exchange. Persist it before
        # it is used, otherwise a crash here leaves only the superseded copy and
        # the next start needs a fresh consent.
        rotated = payload.get("refresh_token")
        if isinstance(rotated, str) and rotated and rotated != self._refresh_token:
            self._settings.write_refresh_token(rotated)
            self._refresh_token = rotated
            logger.info("refresh token rotated and persisted")

        logger.info("access token refreshed, valid for %.0fs", lifetime)
