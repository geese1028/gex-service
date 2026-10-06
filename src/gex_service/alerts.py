"""Alert rules evaluated after every refresh, with WebSocket fan-out and an optional webhook.

Rules are edge-triggered: an alert fires on the refresh where the condition
changes, not on every refresh while it holds.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable

from pydantic import BaseModel, Field

from .models import GexResponse

log = logging.getLogger(__name__)

ALL_RULES = (
    "zero_gamma_cross",
    "call_wall_cross",
    "put_wall_cross",
    "regime_flip",
    "gamma_imbalance_high",
    "vanna_flip_cross",
    "wall_shift",
)


class AlertConfig(BaseModel):
    enabled: bool = True
    rules: list[str] = Field(default_factory=lambda: list(ALL_RULES))
    gamma_imbalance_pct_adv: float = 10.0  # fires when |Gamma^IB| rises through this level
    wall_shift_min_pct: float = 0.0  # ignore wall moves smaller than this % of spot


class Alert(BaseModel):
    id: int | None = None
    ts: datetime
    symbol: str
    rule: str
    message: str
    payload: dict


AlertListener = Callable[[Alert], Awaitable[None]]


def _side(x: float, level: float | None) -> int | None:
    if level is None:
        return None
    return 1 if x > level else (-1 if x < level else 0)


def evaluate(prev: GexResponse | None, cur: GexResponse, cfg: AlertConfig) -> list[Alert]:
    if prev is None or not cfg.enabled:
        return []
    out: list[Alert] = []
    ts = cur.ts if cur.ts.tzinfo else cur.ts.replace(tzinfo=timezone.utc)
    sym = cur.symbol
    ps, cs = prev.summary, cur.summary

    def fire(rule: str, message: str, **payload: object) -> None:
        if rule in cfg.rules:
            out.append(Alert(ts=ts, symbol=sym, rule=rule, message=message, payload={"spot": cur.spot, "prev_spot": prev.spot, **payload}))

    # Crosses use the *current* levels so a level shift alone does not count as a cross.
    for rule, level, label in (
        ("zero_gamma_cross", cs.zero_gamma, "zero gamma"),
        ("call_wall_cross", cs.call_wall, "call wall"),
        ("put_wall_cross", cs.put_wall, "put wall"),
    ):
        a, b = _side(prev.spot, level), _side(cur.spot, level)
        if a is not None and b is not None and a != b and a != 0 and b != 0:
            direction = "above" if b > 0 else "below"
            fire(rule, f"{sym} crossed {direction} {label} {level:g} (spot {prev.spot:g} -> {cur.spot:g})", level=level, direction=direction)

    if prev.hedge_flow and cur.hedge_flow and prev.hedge_flow.regime != cur.hedge_flow.regime:
        fire(
            "regime_flip",
            f"{sym} regime {prev.hedge_flow.regime} -> {cur.hedge_flow.regime} (total GEX {cs.total_gex:,.0f})",
            from_regime=prev.hedge_flow.regime, to_regime=cur.hedge_flow.regime, total_gex=cs.total_gex,
        )

    if prev.hedge_flow and cur.hedge_flow:
        p, c = prev.hedge_flow.pct_adv_per_1pct, cur.hedge_flow.pct_adv_per_1pct
        if p is not None and c is not None and p < cfg.gamma_imbalance_pct_adv <= c:
            fire(
                "gamma_imbalance_high",
                f"{sym} gamma imbalance {c:.1f}% of ADV per 1% move (threshold {cfg.gamma_imbalance_pct_adv:g}%)",
                pct_adv=c, threshold=cfg.gamma_imbalance_pct_adv,
            )

    if prev.exposures and cur.exposures:
        a, b = _side(prev.spot, cur.exposures.vanna_flip), _side(cur.spot, cur.exposures.vanna_flip)
        if a is not None and b is not None and a != b and a != 0 and b != 0:
            fire("vanna_flip_cross", f"{sym} crossed vanna flip {cur.exposures.vanna_flip:g}", level=cur.exposures.vanna_flip)

    for label, a, b in (("call_wall", ps.call_wall, cs.call_wall), ("put_wall", ps.put_wall, cs.put_wall)):
        if a is not None and b is not None and a != b and abs(b - a) / cur.spot * 100 >= cfg.wall_shift_min_pct:
            fire("wall_shift", f"{sym} {label} moved {a:g} -> {b:g}", wall=label, from_level=a, to_level=b)
    return out


class AlertManager:
    def __init__(self, store, webhook_url: str = "") -> None:
        self.store = store
        self.webhook_url = webhook_url
        self._configs: dict[str, AlertConfig] = {}
        self._listeners: set[AlertListener] = set()
        self._prev: dict[str, GexResponse] = {}
        self._http = None

    def config(self, symbol: str) -> AlertConfig:
        return self._configs.get(symbol.upper(), AlertConfig())

    def set_config(self, symbol: str, cfg: AlertConfig) -> None:
        self._configs[symbol.upper()] = cfg

    def configs(self) -> dict[str, AlertConfig]:
        return dict(self._configs)

    def subscribe(self, listener: AlertListener) -> None:
        self._listeners.add(listener)

    def unsubscribe(self, listener: AlertListener) -> None:
        self._listeners.discard(listener)

    async def on_result(self, result: GexResponse) -> list[Alert]:
        prev = self._prev.get(result.symbol)
        self._prev[result.symbol] = result
        alerts = evaluate(prev, result, self.config(result.symbol))
        for alert in alerts:
            try:
                alert.id = await self.store.save_alert(alert.ts, alert.symbol, alert.rule, alert.message, alert.payload)
            except Exception as exc:  # noqa: BLE001
                log.warning("alert save failed: %s", exc)
            log.info("ALERT %s", alert.message)
            for listener in list(self._listeners):
                try:
                    await listener(alert)
                except Exception:  # noqa: BLE001
                    self._listeners.discard(listener)
        if alerts and self.webhook_url:
            asyncio.create_task(self._post_webhook(alerts))
        return alerts

    async def _post_webhook(self, alerts: list[Alert]) -> None:
        try:
            import httpx

            if self._http is None:
                self._http = httpx.AsyncClient(timeout=10.0)
            payload = {
                # Discord/Slack-compatible text plus the structured alerts
                "content": "\n".join(a.message for a in alerts),
                "text": "\n".join(a.message for a in alerts),
                "alerts": [a.model_dump(mode="json") for a in alerts],
            }
            resp = await self._http.post(self.webhook_url, json=payload)
            if resp.status_code >= 300:
                log.warning("alert webhook returned %s", resp.status_code)
        except Exception as exc:  # noqa: BLE001
            log.warning("alert webhook failed: %s", exc)

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
