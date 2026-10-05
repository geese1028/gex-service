"""Runtime settings, loaded from environment variables / .env (prefix ``GEX_``)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GEX_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # IB Gateway
    ib_host: str = "127.0.0.1"
    ib_port: int = 4002
    ib_client_id: int = 41
    ib_connect_timeout_s: float = 15.0
    ib_reconnect_min_s: float = 5.0
    ib_reconnect_max_s: float = 300.0

    # HTTP API
    api_host: str = "127.0.0.1"
    api_port: int = 8090
    api_key: str = ""
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])

    # Chain selection defaults
    max_dte: int = 45
    strike_range_pct: float = 0.12
    max_contracts: int = 1500

    # Market data pacing
    batch_size: int = 60
    batch_wait_s: float = 3.0
    batch_min_wait_s: float = 1.0
    batch_sleep_s: float = 0.5
    details_concurrency: int = 4
    refresh_interval_s: float = 90.0
    fetch_timeout_s: float = 180.0
    idle_ttl_s: float = 1800.0

    # Pricing inputs
    risk_free_rate: float = 0.045
    dividend_yield: float = 0.0

    # Market data type: 1 live, 2 frozen, 3 delayed, 4 delayed-frozen
    market_data_type: int = 1
    auto_frozen: bool = True

    # Storage
    db_path: Path = Path("data/gex.sqlite")
    snapshot_retention_days: int = 14

    log_level: str = "INFO"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("strike_range_pct")
    @classmethod
    def _range_positive(cls, value: float) -> float:
        if not 0 < value <= 1:
            raise ValueError("strike_range_pct must be in (0, 1]")
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
