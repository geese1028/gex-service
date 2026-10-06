"""Runtime settings, loaded from environment variables / .env (prefix ``GEX_``)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GEX_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # IB Gateway via NautilusTrader HistoricInteractiveBrokersClient.
    # Defaults match the adapter: 127.0.0.1:4002, client_id=1.
    ib_host: str = "127.0.0.1"
    ib_port: int = 4002
    ib_client_id: int = 1
    ib_connect_timeout_s: float = 60.0
    ib_reconnect_min_s: float = 5.0
    ib_reconnect_max_s: float = 300.0
    ib_request_timeout_s: int = 120  # raised for large option-chain contract details
    historical_request_delay_s: float = 1.0  # IB historical pacing, not multi-app sharing
    historical_timeout_s: int = 120
    max_md_lines: int = 100  # IB simultaneous reqMktData ceiling
    instrument_cache_path: Path | None = Path("data/ib_instruments.pkl")

    # HTTP API
    api_host: str = "127.0.0.1"
    api_port: int = 8090
    api_key: str = ""
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])

    # Chain selection defaults
    max_dte: int = 45
    strike_range_pct: float = 0.12
    max_contracts: int = 1500

    # Market data pacing against IB's own line / snapshot budget.
    batch_size: int = 100
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

    # Extensions
    eod_archive_time: str = "16:05"  # ET; last snapshot at or before 16:00 ET is archived
    oi_opening_ratio: float = 0.5  # heuristic share of today's volume assumed to open new OI
    flow_max_symbols: int = 2
    flow_strikes_per_side: int = 3
    alert_webhook_url: str = ""
    scan_max_symbols: int = 40

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

    @field_validator("batch_size")
    @classmethod
    def _batch_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("batch_size must be >= 1")
        return value

    @model_validator(mode="after")
    def _cap_batch_to_ib_lines(self) -> Settings:
        if self.batch_size > self.max_md_lines:
            self.batch_size = self.max_md_lines
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
