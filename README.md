# miele-nats-bridge

Bridges Miele kitchen appliances from the Miele 3rd Party Cloud API to NATS JetStream,
so appliance state can be mirrored onto KNX group addresses and archived in TimescaleDB.

```
Miele Cloud (SSE) --> miele-nats-bridge --> miele.<slug>.state
                                        \-> miele.<slug>.eco
```

Miele exposes no local API for the current Wi-Fi appliance generation, so this bridge is
a deliberate exception to an otherwise LAN-local smart-home stack: appliance state goes
stale while the WAN link is down, and nothing safety-relevant may depend on it.

## Subjects

| Subject | Contents |
| --- | --- |
| `miele.<slug>.state` | status, program, phase, durations, temperatures, signals, remote enable |
| `miele.<slug>.eco` | `ecoFeedback` energy and water per programme run |

Payloads are flat named scalars. Fields the appliance does not report are omitted rather
than published as null, so a consumer keeps the last known value.

The cloud repeats the full state of every appliance every few seconds; the bridge
publishes only when a normalized payload actually changed.

## Normalization

The Miele dialect is resolved here so downstream consumers see scalars only:

- **-32768 is the null sentinel** for temperatures, not an omitted field. It is dropped
  instead of being published as -327.68 °C.
- `value_localized` is already in °C, so no 1/100 scaling is applied.
- `remainingTime` / `startTime` / `elapsedTime` arrive as `[hours, minutes]` and become
  plain minutes.
- Program and phase codes are fitted into DPT 5.010 by one subtrahend per appliance. Most
  blocks already fit (dishwasher 1..44, ovens 6..75) and pass through unchanged, so the bus
  value matches Miele's own documentation; only the coffee system's 24000..24050 and the
  phase blocks are shifted. The raw code and plain-text name are published unshifted. See
  `programs.py`.
- Program names contain non-breaking spaces, which are normalized to plain spaces.

## Configuration

Appliances come from a YAML file (ConfigMap), credentials from files (Secret):

```yaml
appliances:
  - device_id: "000105454657"
    name: geschirrspueler
    model: G7560
```

| Env var | Default | Purpose |
| --- | --- | --- |
| `MIELE_APPLIANCES_FILE` | `/etc/miele-nats-bridge/appliances.yaml` | appliance mapping |
| `MIELE_CLIENT_ID_FILE` | `.../credentials/client-id` | OAuth2 client id |
| `MIELE_CLIENT_SECRET_FILE` | `.../credentials/client-secret` | OAuth2 client secret |
| `MIELE_REFRESH_TOKEN_FILE` | `.../credentials/refresh-token` | initial refresh token |
| `MIELE_TOKEN_STATE_FILE` | `/var/lib/miele-nats-bridge/refresh-token` | rotated token (PVC) |
| `NATS_SERVERS` | `nats://localhost:4222` | NATS endpoints |
| `NATS_STREAM_NAME` | `MIELE` | JetStream stream to verify at startup |
| `METRICS_PORT` | `9090` | `/metrics` and `/healthz` |

### Token handling

Miele's Keycloak issues short-lived access tokens (one hour observed) and **rotates the
refresh token on every exchange**. The SealedSecret supplies only the initial refresh
token; every rotation is persisted to the PVC, which then takes precedence. Writing back
into the ArgoCD-managed Secret was rejected — it conflicts with GitOps ownership.

A rejected refresh token is not retryable and exits the process rather than looping, since
only a new consent round can produce a working one.

## Bootstrap

```
uv run miele-cloud-auth --client-id <id> \
    --refresh-token-out refresh-token \
    --devices-out devices.json
```

Requests `openid mcs_thirdparty_read mcs_thirdparty_write` in one consent round, so the
command direction needs no second consent. All appliances must be approved at consent
time.

`--programs-out-dir` additionally dumps `GET /devices/{id}/programs` per appliance. This is
**not part of the normal bootstrap**: the endpoint is only answered while an appliance is
switched on, and it is not reliably free of side effects — an appliance was observed
switching itself on and starting a cycle in response. Treat it as a read that can touch the
hardware: pass it deliberately, with the appliances already on, never as routine.

The `http://localhost:8080/callback` redirect URI is accepted by Miele's Keycloak as-is;
no registration of the URI is required. Safari's HTTPS-Only mode blocks the callback
though — use another browser, or pass `--manual` and paste the redirected URL back in.

## Metrics

`miele_connected`, `miele_token_expiry_timestamp_seconds`, `miele_events_total`,
`miele_api_errors_total`, `miele_resyncs_total`, `miele_messages_published_total`,
`miele_publish_errors_total`, `nats_connected`.

Liveness deliberately excludes cloud reachability: a Miele outage sets `miele_connected 0`
instead of restart-looping the pod.

## Development

```
uv sync --extra dev
uv run ruff check .
uv run mypy src
uv run pytest -q
```
