"""Token refresh and rotation: the part that silently breaks a month after deployment."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from miele_nats_bridge import auth
from miele_nats_bridge.auth import ConsentRequiredError, TokenManager
from miele_nats_bridge.config import TOKEN_URL, Settings
from miele_nats_bridge.metrics import Metrics


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    (credentials / "client-id").write_text("client-abc")
    (credentials / "client-secret").write_text("secret-xyz")
    (credentials / "refresh-token").write_text("seed-refresh-token")
    return Settings(
        miele_client_id_file=credentials / "client-id",
        miele_client_secret_file=credentials / "client-secret",
        miele_refresh_token_file=credentials / "refresh-token",
        miele_token_state_file=tmp_path / "state" / "refresh-token",
    )


def token_response(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "access_token": "access-1",
        "refresh_token": "rotated-1",
        "expires_in": 3600,
        "token_type": "Bearer",
    }
    payload.update(overrides)
    return payload


@pytest.fixture(autouse=True)
def no_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the retry paths instant; the delays themselves are not under test."""

    async def instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(auth.asyncio, "sleep", instant)


@respx.mock
async def test_refresh_returns_access_token_and_sends_credentials(settings: Settings) -> None:
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=token_response()))
    async with httpx.AsyncClient() as http:
        tokens = TokenManager(settings, Metrics(), http)
        assert await tokens.access_token() == "access-1"

    request = route.calls.last.request
    body = dict(pair.split("=", 1) for pair in request.content.decode().split("&"))
    assert body["grant_type"] == "refresh_token"
    assert body["client_id"] == "client-abc"
    assert body["refresh_token"] == "seed-refresh-token"


@respx.mock
async def test_rotated_refresh_token_is_persisted(settings: Settings) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=token_response()))
    async with httpx.AsyncClient() as http:
        await TokenManager(settings, Metrics(), http).access_token()

    # The rotated token must survive a restart, otherwise the next start
    # presents a superseded token and needs a fresh consent round.
    assert settings.miele_token_state_file.read_text() == "rotated-1"
    assert settings.miele_token_state_file.stat().st_mode & 0o777 == 0o600


@respx.mock
async def test_persisted_token_wins_over_the_secret_seed(settings: Settings) -> None:
    settings.miele_token_state_file.parent.mkdir(parents=True)
    settings.miele_token_state_file.write_text("token-from-pvc")
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=token_response()))

    async with httpx.AsyncClient() as http:
        await TokenManager(settings, Metrics(), http).access_token()

    body = route.calls.last.request.content.decode()
    assert "refresh_token=token-from-pvc" in body


@respx.mock
async def test_token_is_reused_until_the_refresh_margin(settings: Settings) -> None:
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=token_response()))
    async with httpx.AsyncClient() as http:
        tokens = TokenManager(settings, Metrics(), http)
        assert await tokens.access_token() == "access-1"
        assert await tokens.access_token() == "access-1"
    assert route.call_count == 1


@respx.mock
async def test_expiring_token_triggers_a_second_refresh(settings: Settings) -> None:
    route = respx.post(TOKEN_URL).mock(
        side_effect=[
            httpx.Response(200, json=token_response(expires_in=10)),
            httpx.Response(200, json=token_response(access_token="access-2")),
        ]
    )
    async with httpx.AsyncClient() as http:
        tokens = TokenManager(settings, Metrics(), http)
        # expires_in 10s is well inside the 300s margin, so the next read refreshes.
        assert await tokens.access_token() == "access-1"
        assert await tokens.access_token() == "access-2"
    assert route.call_count == 2


@respx.mock
@pytest.mark.parametrize("status", [400, 401])
async def test_rejected_refresh_token_is_not_retried(settings: Settings, status: int) -> None:
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(status, json={"error": "invalid_grant"})
    )
    async with httpx.AsyncClient() as http:
        tokens = TokenManager(settings, Metrics(), http)
        with pytest.raises(ConsentRequiredError):
            await tokens.access_token()
    assert route.call_count == 1


@respx.mock
async def test_server_error_is_retried_then_gives_up(settings: Settings) -> None:
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as http:
        tokens = TokenManager(settings, Metrics(), http)
        with pytest.raises(RuntimeError, match="after 5 attempts"):
            await tokens.access_token()
    assert route.call_count == 5


@respx.mock
async def test_transient_failure_recovers_on_a_later_attempt(settings: Settings) -> None:
    route = respx.post(TOKEN_URL).mock(
        side_effect=[
            httpx.ConnectError("boom"),
            httpx.Response(500),
            httpx.Response(200, json=token_response()),
        ]
    )
    async with httpx.AsyncClient() as http:
        tokens = TokenManager(settings, Metrics(), http)
        assert await tokens.access_token() == "access-1"
    assert route.call_count == 3


@respx.mock
async def test_response_without_access_token_fails(settings: Settings) -> None:
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"refresh_token": "rotated-1"})
    )
    async with httpx.AsyncClient() as http:
        tokens = TokenManager(settings, Metrics(), http)
        with pytest.raises(RuntimeError, match="no access_token"):
            await tokens.access_token()


@respx.mock
async def test_unchanged_refresh_token_is_not_rewritten(settings: Settings) -> None:
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json=token_response(refresh_token="seed-refresh-token"))
    )
    async with httpx.AsyncClient() as http:
        await TokenManager(settings, Metrics(), http).access_token()
    # Nothing rotated, so the PVC copy is never created.
    assert not settings.miele_token_state_file.exists()


@respx.mock
async def test_concurrent_reads_refresh_only_once(settings: Settings) -> None:
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=token_response()))
    async with httpx.AsyncClient() as http:
        tokens = TokenManager(settings, Metrics(), http)
        results = await asyncio.gather(*(tokens.access_token() for _ in range(5)))
    assert results == ["access-1"] * 5
    assert route.call_count == 1


@respx.mock
async def test_expiry_metric_tracks_the_new_token(settings: Settings) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=token_response()))
    metrics = Metrics()
    before = time.time()
    async with httpx.AsyncClient() as http:
        await TokenManager(settings, metrics, http).access_token()

    expiry = metrics.registry.get_sample_value("miele_token_expiry_timestamp_seconds")
    assert expiry is not None
    assert before + 3500 <= expiry <= time.time() + 3600
