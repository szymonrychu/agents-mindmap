"""MINDMAP_* settings of agents-mindmap."""

import ipaddress
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from agents_common.settings import BaseServiceSettings, CsvList, SettingsError
from agents_common.settings import load_settings as _load
from pydantic import Field, SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import NoDecode, SettingsConfigDict

__all__ = ["EMBED_DIM", "EMBED_MODEL", "Settings", "SettingsError", "load_settings"]

# The embedding model and its dimension. `serve` and `embedder` compare both with the
# `embedding_config` row at start and refuse to start on a mismatch.
EMBED_MODEL = "intfloat/multilingual-e5-large-instruct"
EMBED_DIM = 1024

Port = Annotated[int, Field(ge=1, le=65535)]


def _absolute_url(value: str, env: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"{env}::not an absolute http(s) URL")
    return value.rstrip("/")


class Settings(BaseServiceSettings):
    """Environment-driven settings shared by every process (serve, migrate, workers)."""

    model_config = SettingsConfigDict(env_prefix="MINDMAP_", extra="ignore")

    allowed_cidrs: CsvList = ["127.0.0.1/32"]
    http_port: Port = 8080
    delegate_port: Port = 8081
    service_port: Port = 8082
    public_base_url: str
    orchestrator_url: str
    relay_token: SecretStr = Field(SecretStr(""), repr=False)
    delegate_tokens: Annotated[list[SecretStr], NoDecode] = Field([], repr=False)
    orchestrator_token: SecretStr = Field(SecretStr(""), repr=False)
    taskboard_url: str = ""
    taskboard_token: SecretStr = Field(SecretStr(""), repr=False)
    embedder_url: str = ""
    embedder_model: str = EMBED_MODEL
    embed_dim: int = Field(EMBED_DIM, ge=1, le=16000)
    embedder_timeout_seconds: float = Field(120.0, gt=0)
    embedder_batch_size: int = Field(8, ge=1, le=64)
    embedder_poll_seconds: float = Field(5.0, gt=0)
    embedder_backoff_max_seconds: float = Field(300.0, gt=0)
    metrics_port: Port = 9102
    notice_debounce_seconds: float = Field(60.0, ge=0)
    notifier_poll_seconds: float = Field(5.0, gt=0)
    notifier_backoff_max_seconds: float = Field(60.0, gt=0)
    idle_in_transaction_timeout_ms: int = Field(30000, ge=1)

    @field_validator("allowed_cidrs")
    @classmethod
    def _cidrs(cls, value: list[str], info: ValidationInfo) -> list[str]:
        for item in value:
            try:
                ipaddress.ip_network(item, strict=False)
            except ValueError:
                raise ValueError(
                    f"MINDMAP_{str(info.field_name).upper()}::not a CIDR: {item!r}"
                ) from None
        return value

    @field_validator("public_base_url")
    @classmethod
    def _base_url(cls, value: str) -> str:
        return _absolute_url(value, "MINDMAP_PUBLIC_BASE_URL")

    @field_validator("orchestrator_url")
    @classmethod
    def _orchestrator_url(cls, value: str) -> str:
        return _absolute_url(value, "MINDMAP_ORCHESTRATOR_URL")

    @field_validator("taskboard_url", "embedder_url")
    @classmethod
    def _optional_url(cls, value: str, info: ValidationInfo) -> str:
        if not value:
            return value
        return _absolute_url(value, f"MINDMAP_{str(info.field_name).upper()}")

    @model_validator(mode="after")
    def _token_from_file(self) -> "Settings":
        """Replaces the agents-common validator of the same name: reads
        MINDMAP_ORCHESTRATOR_TOKEN_FILE the same way but leaves the token optional, because
        migrate, embedder and reembed never call the orchestrator and their pods do not hold the
        token (secrets invariant 9). The commands that call it require it (cli)."""
        if self.orchestrator_token_file:
            try:
                text = Path(self.orchestrator_token_file).read_text().strip()
            except OSError as exc:
                raise ValueError(
                    f"MINDMAP_ORCHESTRATOR_TOKEN_FILE::cannot read: {exc.strerror}"
                ) from None
            self.orchestrator_token = SecretStr(text)
        return self

    @model_validator(mode="after")
    def _ports(self) -> "Settings":
        if self.delegate_port == self.http_port:
            raise ValueError("MINDMAP_DELEGATE_PORT::must differ from MINDMAP_HTTP_PORT")
        if self.service_port in (self.http_port, self.delegate_port):
            raise ValueError(
                "MINDMAP_SERVICE_PORT::must differ from MINDMAP_HTTP_PORT and MINDMAP_DELEGATE_PORT"
            )
        return self

    @model_validator(mode="after")
    def _tokens(self) -> "Settings":
        """Every token this service holds is distinct (secrets invariant 9). Errors name the
        env vars only, never a value."""
        if self.taskboard_url and not self.taskboard_token.get_secret_value():
            raise ValueError("MINDMAP_TASKBOARD_TOKEN::required when MINDMAP_TASKBOARD_URL is set")
        seen: dict[str, str] = {}
        named = [
            ("MINDMAP_RELAY_TOKEN", self.relay_token),
            ("MINDMAP_ORCHESTRATOR_TOKEN", self.orchestrator_token),
            *(("MINDMAP_DELEGATE_TOKENS", t) for t in self.delegate_tokens),
            ("MINDMAP_TASKBOARD_TOKEN", self.taskboard_token),
        ]
        for env, token in named:
            value = token.get_secret_value()
            if not value:
                continue
            if value in seen:
                raise ValueError(f"{env}::must differ from {seen[value]}")
            seen[value] = env
        return self

    def api_networks(self) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
        return [ipaddress.ip_network(c, strict=False) for c in self.allowed_cidrs]


def load_settings() -> Settings:
    """Load Settings from the environment or raise SettingsError naming every bad variable."""
    return _load(Settings)
