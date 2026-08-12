"""One-time consent bootstrap: OAuth2 authorization code flow -> refresh token + API dump.

Run locally, never in the cluster:

    uv run miele-cloud-auth --client-id <id> --refresh-token-out refresh-token

The refresh token it prints is what the SealedSecret seeds; the bridge persists
every rotation itself. The same run also dumps ``GET /devices`` and, for every
appliance that is switched on, ``GET /devices/{id}/programs`` — the input for the
program compaction tables.

Safari's HTTPS-Only mode blocks the ``http://localhost`` callback. Use another
browser, or pass ``--manual`` and paste the redirected URL back in.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import http.server
import json
import secrets
import sys
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any

import httpx

from ..config import API_BASE, AUTH_URL, SCOPES, TOKEN_URL

_CALLBACK_TIMEOUT_SECONDS = 300.0
_HTTP_TIMEOUT = 30.0

_SUCCESS_PAGE = b"""<!doctype html><meta charset="utf-8">
<title>Consent granted</title>
<body style="font-family:system-ui;padding:3rem">
<h1>Consent granted</h1><p>You can close this tab and return to the terminal.</p>
"""


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Captures the authorization code from the single redirect we expect."""

    query: dict[str, list[str]] = {}

    def do_GET(self) -> None:  # noqa: N802 (stdlib override)
        _CallbackHandler.query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(_SUCCESS_PAGE)))
        self.end_headers()
        self.wfile.write(_SUCCESS_PAGE)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 (stdlib override)
        """Silence the default stderr access log."""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-id", required=True, help="OAuth2 client id from Miele")
    parser.add_argument(
        "--client-secret", help="OAuth2 client secret; prompted interactively when omitted"
    )
    parser.add_argument(
        "--redirect-uri",
        default="http://localhost:8080/callback",
        help="Redirect URI; must match what the authorization server accepts",
    )
    parser.add_argument("--scope", default=SCOPES, help=f"Requested scopes (default: {SCOPES})")
    parser.add_argument(
        "--manual",
        action="store_true",
        help="Skip the local listener and paste the redirected URL instead",
    )
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser")
    parser.add_argument("--refresh-token-out", help="Write the refresh token here (mode 0600)")
    parser.add_argument("--devices-out", help="Write the GET /devices dump here")
    parser.add_argument("--programs-out-dir", help="Write per-appliance program lists here")
    parser.add_argument("--language", default="de", help="Accept-Language value (default: de)")
    args = parser.parse_args()

    client_secret = args.client_secret or getpass.getpass("Miele client secret: ")

    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )
    state = secrets.token_urlsafe(16)

    auth_url = f"{AUTH_URL}?" + urllib.parse.urlencode(
        {
            "client_id": args.client_id,
            "response_type": "code",
            "redirect_uri": args.redirect_uri,
            "scope": args.scope,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )

    print("Open this URL and approve all appliances:\n")
    print(auth_url)
    print()
    if not args.no_browser:
        webbrowser.open(auth_url)

    code = _manual_code(state) if args.manual else _listen_for_code(args.redirect_uri, state)

    with httpx.Client(timeout=_HTTP_TIMEOUT) as http:
        tokens = _exchange(http, args, client_secret, code, verifier)
        refresh_token = tokens.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            sys.exit("token response contained no refresh_token")

        print(f"\naccess token valid for {tokens.get('expires_in', '?')}s")
        if args.refresh_token_out:
            out = Path(args.refresh_token_out)
            out.write_text(refresh_token)
            out.chmod(0o600)
            print(f"refresh token written to {out}")
        else:
            print(f"refresh token: {refresh_token}")

        _dump(http, args, str(tokens["access_token"]))


def _listen_for_code(redirect_uri: str, state: str) -> str:
    parsed = urllib.parse.urlparse(redirect_uri)
    address = (parsed.hostname or "localhost", parsed.port or 80)
    server = http.server.HTTPServer(address, _CallbackHandler)
    server.timeout = _CALLBACK_TIMEOUT_SECONDS
    print(f"waiting for the redirect on {redirect_uri} ...")
    server.handle_request()
    server.server_close()
    return _code_from_query(_CallbackHandler.query, state)


def _manual_code(state: str) -> str:
    pasted = input("paste the full redirected URL (or just the code): ").strip()
    if pasted.startswith("http"):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query)
        return _code_from_query(query, state)
    if not pasted:
        sys.exit("no code supplied")
    return pasted


def _code_from_query(query: dict[str, list[str]], state: str) -> str:
    if "error" in query:
        description = query.get("error_description", [""])[0]
        sys.exit(f"authorization failed: {query['error'][0]} {description}")
    returned_state = query.get("state", [""])[0]
    if returned_state != state:
        # A mismatch means the redirect did not belong to this run.
        sys.exit("state mismatch between request and redirect")
    code = query.get("code", [""])[0]
    if not code:
        sys.exit("redirect carried no authorization code")
    return code


def _exchange(
    http_client: httpx.Client,
    args: argparse.Namespace,
    client_secret: str,
    code: str,
    verifier: str,
) -> dict[str, Any]:
    response = http_client.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": args.client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": args.redirect_uri,
            "code_verifier": verifier,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if response.status_code != httpx.codes.OK:
        sys.exit(f"token exchange failed: HTTP {response.status_code} {response.text[:400]}")
    payload: dict[str, Any] = response.json()
    return payload


def _dump(http_client: httpx.Client, args: argparse.Namespace, access_token: str) -> None:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Accept-Language": args.language,
    }

    response = http_client.get(f"{API_BASE}/devices", headers=headers)
    if response.status_code != httpx.codes.OK:
        print(f"GET /devices failed: HTTP {response.status_code}", file=sys.stderr)
        return
    devices: dict[str, Any] = response.json()
    print(f"\n{len(devices)} appliance(s):")
    for device_id, entry in devices.items():
        ident = entry.get("ident", {})
        label = ident.get("deviceIdentLabel", {})
        state = entry.get("state", {}).get("status", {})
        print(
            f"  {device_id}  type={ident.get('type', {}).get('value_raw')}"
            f"  {label.get('techType', '?')}  status={state.get('value_localized', '?')}"
        )

    if args.devices_out:
        Path(args.devices_out).write_text(json.dumps(devices, indent=2, ensure_ascii=False))
        print(f"devices dump written to {args.devices_out}")

    if not args.programs_out_dir:
        return

    out_dir = Path(args.programs_out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    print("\nprogram lists (appliances must be switched on):")
    for device_id in devices:
        response = http_client.get(f"{API_BASE}/devices/{device_id}/programs", headers=headers)
        if response.status_code != httpx.codes.OK:
            print(f"  {device_id}: HTTP {response.status_code} — switched off?")
            continue
        programs = response.json()
        path = out_dir / f"{device_id}.json"
        path.write_text(json.dumps(programs, indent=2, ensure_ascii=False))
        print(f"  {device_id}: {len(programs)} program(s) -> {path}")
