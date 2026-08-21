"""Settings from env vars (pydantic-settings); appliances from YAML, secrets from files."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Miele moved third-party OAuth to Keycloak; the legacy api.mcs3.miele.com/thirdparty
# endpoints are announced for deactivation during 2026 and are not used here.
AUTH_URL = "https://auth.domestic.miele-iot.com/partner/realms/mcs/protocol/openid-connect/auth"
TOKEN_URL = "https://auth.domestic.miele-iot.com/partner/realms/mcs/protocol/openid-connect/token"
API_BASE = "https://api.mcs3.miele.com/v1"

# Read is what the bridge needs; write is requested at consent time so the command
# path can reuse the same refresh token without a second consent round.
SCOPES = "openid mcs_thirdparty_read mcs_thirdparty_write"


class LogFormat(StrEnum):
    JSON = "json"
    TEXT = "text"


class ApplianceConfig(BaseModel):
    """One Miele appliance: cloud identity plus its NATS subject namespace."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Fabrication number from the API. Keyed on deviceId rather than deviceType
    # because a second appliance of the same type would make type-keying ambiguous.
    device_id: str
    # Stable slug used in NATS subjects (miele.<name>.state).
    name: str
    # Free-text, only for log lines; the API reports deviceName as empty.
    model: str = ""
    subject_prefix: str = "miele"

    @field_validator("name", "subject_prefix")
    @classmethod
    def _single_token(cls, v: str) -> str:
        if "." in v or "/" in v or " " in v or not v:
            raise ValueError("must be a non-empty single token (no dots, slashes, spaces)")
        return v

    @property
    def state_subject(self) -> str:
        return f"{self.subject_prefix}.{self.name}.state"

    @property
    def eco_subject(self) -> str:
        return f"{self.subject_prefix}.{self.name}.eco"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Appliances: non-secret mapping in a YAML file (ConfigMap).
    miele_appliances_file: Path = Path("/etc/miele-nats-bridge/appliances.yaml")

    # OAuth2 client credentials (Secret).
    miele_client_id_file: Path = Path("/etc/miele-nats-bridge/credentials/client-id")
    miele_client_secret_file: Path = Path("/etc/miele-nats-bridge/credentials/client-secret")
    # Initial refresh token from the SealedSecret; only read when the persisted
    # one below is absent.
    miele_refresh_token_file: Path = Path("/etc/miele-nats-bridge/credentials/refresh-token")
    # Rotated refresh token, persisted on a PVC. Writing back into the
    # ArgoCD-managed Secret would conflict with GitOps ownership.
    miele_token_state_file: Path = Path("/var/lib/miele-nats-bridge/refresh-token")

    api_base: str = API_BASE
    token_url: str = TOKEN_URL
    # The events endpoint ignores ?language=; only the Accept-Language header works.
    language: str = "de"

    # Refresh this many seconds before the access token actually expires.
    token_refresh_margin_seconds: float = 300.0
    # No periodic resync: the stream repeats the full state of every appliance
    # every few seconds, so a timed GET /devices would only burn rate limit.
    sse_backoff_initial_seconds: float = 5.0
    sse_backoff_max_seconds: float = 300.0

    # Smallest temperature movement worth a publish, in °C. Appliances report
    # 1/100 °C and drift continuously even while switched off. 0 disables it.
    temperature_min_delta_c: float = 0.5

    # NATS
    nats_servers: str = "nats://localhost:4222"
    nats_subject_prefix: str = "miele"
    nats_creds_file: Path | None = None
    nats_nkey_seed_file: Path | None = None
    nats_user: str | None = None
    nats_user_password_file: Path | None = None
    nats_stream_check: bool = True
    nats_stream_name: str = "MIELE"

    # Observability
    metrics_port: int = 9090
    log_level: str = "INFO"
    log_format: LogFormat = LogFormat.JSON

    @property
    def nats_servers_list(self) -> list[str]:
        return [s.strip() for s in self.nats_servers.split(",") if s.strip()]

    @field_validator("nats_subject_prefix")
    @classmethod
    def _single_token(cls, v: str) -> str:
        if "." in v or "/" in v or " " in v or not v:
            raise ValueError("must be a non-empty single token (no dots, slashes, spaces)")
        return v

    @field_validator("token_refresh_margin_seconds", "sse_backoff_initial_seconds")
    @classmethod
    def _positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("must be > 0 seconds")
        return v

    @field_validator("temperature_min_delta_c")
    @classmethod
    def _non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError("must be >= 0 °C")
        return v

    def load_appliances(self) -> list[ApplianceConfig]:
        """Parse the appliances YAML; raises on an empty list or duplicate ids/names."""
        if not self.miele_appliances_file.exists():
            raise RuntimeError(f"MIELE_APPLIANCES_FILE {self.miele_appliances_file} does not exist")
        data: Any = yaml.safe_load(self.miele_appliances_file.read_text()) or {}
        if not isinstance(data, dict) or not isinstance(data.get("appliances"), list):
            raise RuntimeError(
                f"{self.miele_appliances_file} must contain a top-level 'appliances' list"
            )

        appliances: list[ApplianceConfig] = []
        for entry in data["appliances"]:
            if not isinstance(entry, dict):
                raise RuntimeError(
                    f"{self.miele_appliances_file}: each appliance must be a mapping"
                )
            appliances.append(
                ApplianceConfig(**{**entry, "subject_prefix": self.nats_subject_prefix})
            )
        if not appliances:
            raise RuntimeError(f"{self.miele_appliances_file} declares no appliances")

        for field in ("name", "device_id"):
            values = [getattr(a, field) for a in appliances]
            duplicates = sorted({v for v in values if values.count(v) > 1})
            if duplicates:
                raise RuntimeError(
                    f"duplicate {field} in {self.miele_appliances_file}: {duplicates}"
                )
        return appliances

    def read_client_id(self) -> str:
        return self._read_secret(self.miele_client_id_file, "MIELE_CLIENT_ID_FILE")

    def read_client_secret(self) -> str:
        return self._read_secret(self.miele_client_secret_file, "MIELE_CLIENT_SECRET_FILE")

    def read_refresh_token(self) -> str:
        """Prefer the rotated token on the PVC; fall back to the Secret's seed.

        The seed is only correct on first start. After the first refresh the
        Secret's copy is stale, so the persisted file always wins when present.
        """
        if self.miele_token_state_file.exists():
            token = self.miele_token_state_file.read_text().strip()
            if token:
                return token
        return self._read_secret(self.miele_refresh_token_file, "MIELE_REFRESH_TOKEN_FILE")

    def write_refresh_token(self, token: str) -> None:
        self.miele_token_state_file.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename so a crash mid-write cannot truncate the only good copy.
        tmp = self.miele_token_state_file.with_suffix(".tmp")
        tmp.write_text(token)
        tmp.chmod(0o600)
        tmp.replace(self.miele_token_state_file)

    @staticmethod
    def _read_secret(path: Path, env_name: str) -> str:
        if not path.exists():
            raise RuntimeError(f"{env_name} {path} does not exist")
        value = path.read_text().strip()
        if not value:
            raise RuntimeError(f"{env_name} {path} is empty")
        return value

    def read_nats_password(self) -> str | None:
        if self.nats_user_password_file and self.nats_user_password_file.exists():
            return self.nats_user_password_file.read_text().strip()
        return None

    def nats_auth_kwargs(self) -> dict[str, Any]:
        """Build the auth subset of NatsClient.connect kwargs.

        Auth precedence: creds file > nkey seed file > user/password.
        Each form is mutually exclusive in nats-py; pick the first that's configured.
        """
        kwargs: dict[str, Any] = {}
        if self.nats_creds_file and self.nats_creds_file.exists():
            kwargs["user_credentials"] = str(self.nats_creds_file)
        elif self.nats_nkey_seed_file and self.nats_nkey_seed_file.exists():
            kwargs["nkeys_seed"] = str(self.nats_nkey_seed_file)
        elif self.nats_user:
            password = self.read_nats_password()
            if password is None:
                raise RuntimeError(
                    "NATS_USER is set but NATS_USER_PASSWORD_FILE is missing or empty"
                )
            kwargs["user"] = self.nats_user
            kwargs["password"] = password
        return kwargs
