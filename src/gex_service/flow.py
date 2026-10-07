"""Trade-by-trade option flow with Lee-Ready classification.

IBKR does not offer tick-by-tick (``reqTickByTickData``) for US options, so
for a handful of near-the-money contracts we stream Nautilus ``reqMktData``
with generic tick 233 (RTVolume). Each print is classified against the
prevailing quote. Customer buys make dealers short the contract, so dealer
gamma from flow is

    flow_gex = -(buy_volume - sell_volume) * gamma * multiplier * S^2 * 0.01

This is opt-in and budgeted against IB's simultaneous market-data line ceiling.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from nautilus_trader.adapters.interactive_brokers.common import IBContract
from pydantic import BaseModel

from .chain import NY, ChainRow, ChainSnapshot
from .greeks import bs_delta, bs_gamma, years_from_days
from .ib_client import IBClient, QuoteSnapshot
from .models import GexResponse, WallTape

log = logging.getLogger(__name__)

# 106 = option implied vol / model greeks, 233 = RTVolume (one entry per print)
FLOW_GENERIC_TICKS = "106,233"


def classify(price: float, bid: float | None, ask: float | None, prev_price: float | None) -> int:
    """Lee-Ready: +1 buy, -1 sell, 0 unclassified."""
    if bid is not None and ask is not None and bid > 0 and ask >= bid:
        if price >= ask:
            return 1
        if price <= bid:
            return -1
        mid = (bid + ask) / 2
        if price > mid:
            return 1
        if price < mid:
            return -1
    if prev_price is not None:
        if price > prev_price:
            return 1
        if price < prev_price:
            return -1
    return 0


@dataclass
class ContractFlow:
    row: ChainRow
    contract: IBContract
    quote: QuoteSnapshot | None = None
    buy_volume: float = 0.0
    sell_volume: float = 0.0
    unclassified_volume: float = 0.0
    trades: int = 0
    last_price: float | None = None
    last_trade_ts: datetime | None = None
    seen: set[tuple[float, float, float]] = field(default_factory=set)

    def gamma(self, spot: float, r: float, q: float) -> float:
        if self.quote is not None and self.quote.gamma is not None:
            return float(self.quote.gamma)
        if self.row.gamma:
            return self.row.gamma
        if self.row.iv:
            return float(bs_gamma(spot, self.row.strike, years_from_days(self.row.dte), self.row.iv, r, q))
        return 0.0

    def delta(self, spot: float, r: float, q: float) -> float:
        if self.quote is not None and self.quote.delta is not None:
            return float(self.quote.delta)
        if self.row.delta:
            return self.row.delta
        if self.row.iv:
            return float(
                bs_delta(spot, self.row.strike, years_from_days(self.row.dte), self.row.iv, self.row.right == "C", r, q)
            )
        return 0.0


class ContractFlowOut(BaseModel):
    con_id: int
    expiry: str
    strike: float
    right: str
    buy_volume: float
    sell_volume: float
    unclassified_volume: float
    net_customer_volume: float
    trades: int
    last_price: float | None
    last_trade_ts: datetime | None
    gamma: float
    flow_gex: float  # dealer gamma added by today's classified flow


class FlowResponse(BaseModel):
    symbol: str
    spot: float
    started_ts: datetime
    contracts: int
    lines_in_use: int
    total_buy_volume: float
    total_sell_volume: float
    total_unclassified_volume: float
    net_flow_gex: float
    net_flow_gex_calls: float
    net_flow_gex_puts: float
    by_contract: list[ContractFlowOut]
    note: str


def select_flow_contracts(snapshot: ChainSnapshot, per_side: int = 3) -> list[ChainRow]:
    """Nearest live expiry, ``per_side`` strikes above and below spot, both rights."""
    live = [r for r in snapshot.rows if r.dte > 0.02]
    if not live:
        return []
    expiry = min(live, key=lambda r: r.dte).expiry
    rows = [r for r in live if r.expiry == expiry]
    strikes = sorted({r.strike for r in rows})
    below = [k for k in strikes if k <= snapshot.spot][-per_side:]
    above = [k for k in strikes if k > snapshot.spot][:per_side]
    keep = set(below + above)
    return [r for r in rows if r.strike in keep]


class SymbolFlow:
    def __init__(self, symbol: str, spot: float, client: IBClient, contracts: list[tuple[ChainRow, IBContract]], r: float, q: float) -> None:
        self.symbol = symbol
        self.spot = spot
        self.client = client
        self.r, self.q = r, q
        self.started_ts = datetime.now(tz=timezone.utc)
        self.session_day: date = self.started_ts.astimezone(NY).date()
        self.key: tuple = tuple(sorted((row.expiry, row.strike, row.right, row.con_id) for row, _ in contracts))
        self.flows: dict[int, ContractFlow] = {row.con_id: ContractFlow(row=row, contract=c) for row, c in contracts}

    async def start(self) -> None:
        for cf in self.flows.values():
            cf.quote = await self.client.subscribe(cf.contract, FLOW_GENERIC_TICKS)
            self.client.on_quote(cf.contract, lambda quote, cf=cf: self._on_quote(cf, quote))
        log.info("flow %s: tracking %d contracts", self.symbol, len(self.flows))

    async def stop(self) -> None:
        for cf in self.flows.values():
            try:
                await self.client.unsubscribe(cf.contract)
            except Exception as exc:  # noqa: BLE001
                log.debug("flow unsubscribe failed: %s", exc)

    def _on_quote(self, cf: ContractFlow, quote: QuoteSnapshot) -> None:
        cf.quote = quote
        for ts, price, size in quote.rt_prints:
            key = (ts, price, size)
            if key in cf.seen:
                continue
            cf.seen.add(key)
            when = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None
            self.record(cf, price, size, when)

    def record(self, cf: ContractFlow, price: float, size: float, ts: datetime | None) -> None:
        bid = ask = None
        if cf.quote is not None:
            bid = cf.quote.bid if cf.quote.bid is not None and cf.quote.bid > 0 else None
            ask = cf.quote.ask if cf.quote.ask is not None and cf.quote.ask > 0 else None
        side = classify(price, bid, ask, cf.last_price)
        if side > 0:
            cf.buy_volume += size
        elif side < 0:
            cf.sell_volume += size
        else:
            cf.unclassified_volume += size
        cf.trades += 1
        cf.last_price = price
        cf.last_trade_ts = ts

    def update_spot(self, spot: float) -> None:
        self.spot = spot

    def wall_tape(self, result: GexResponse) -> WallTape:
        from .gex import front_week_expiry
        from .tape import dealer_gex, dealer_shares, wall_tape_from_legs

        day = result.ts.astimezone(NY).date()
        front = front_week_expiry(result.by_expiry, day)
        expiry = front.expiry if front is not None else None
        call_wall = front.call_wall if front is not None else result.summary.call_wall
        call_buy = call_sell = put_buy = put_sell = 0.0
        gex_sum = share_sum = 0.0
        strikes: set[float] = set()
        for cf in self.flows.values():
            if expiry is not None and cf.row.expiry != expiry:
                continue
            strikes.add(cf.row.strike)
            if cf.row.right == "C":
                call_buy += cf.buy_volume
                call_sell += cf.sell_volume
            else:
                put_buy += cf.buy_volume
                put_sell += cf.sell_volume
            net = cf.buy_volume - cf.sell_volume
            gex_sum += dealer_gex(net, cf.gamma(self.spot, self.r, self.q), cf.row.multiplier, self.spot)
            share_sum += dealer_shares(net, cf.delta(self.spot, self.r, self.q), cf.row.multiplier)
        return wall_tape_from_legs(
            expiry=expiry,
            call_wall=call_wall,
            strikes=sorted(strikes),
            call_buy=call_buy,
            call_sell=call_sell,
            put_buy=put_buy,
            put_sell=put_sell,
            gex=gex_sum,
            shares=share_sum,
        )

    def response(self) -> FlowResponse:
        out: list[ContractFlowOut] = []
        calls = puts = 0.0
        for cf in self.flows.values():
            g = cf.gamma(self.spot, self.r, self.q)
            net_cust = cf.buy_volume - cf.sell_volume
            flow_gex = -net_cust * g * cf.row.multiplier * self.spot * self.spot * 0.01
            if cf.row.right == "C":
                calls += flow_gex
            else:
                puts += flow_gex
            out.append(
                ContractFlowOut(
                    con_id=cf.row.con_id, expiry=cf.row.expiry, strike=cf.row.strike, right=cf.row.right,
                    buy_volume=cf.buy_volume, sell_volume=cf.sell_volume, unclassified_volume=cf.unclassified_volume,
                    net_customer_volume=net_cust, trades=cf.trades, last_price=cf.last_price, last_trade_ts=cf.last_trade_ts,
                    gamma=g, flow_gex=flow_gex,
                )
            )
        out.sort(key=lambda c: (c.strike, c.right))
        return FlowResponse(
            symbol=self.symbol, spot=self.spot, started_ts=self.started_ts, contracts=len(out),
            lines_in_use=self.client.lines_in_use,
            total_buy_volume=sum(c.buy_volume for c in out), total_sell_volume=sum(c.sell_volume for c in out),
            total_unclassified_volume=sum(c.unclassified_volume for c in out),
            net_flow_gex=calls + puts, net_flow_gex_calls=calls, net_flow_gex_puts=puts, by_contract=out,
            note="Lee-Ready classification of RTVolume prints since started_ts on near-ATM contracts of the nearest expiry."
            " Positive flow_gex = customers net sold the contract (dealers long gamma).",
        )


class FlowTracker:
    def __init__(self, client: IBClient, max_symbols: int = 0, per_side: int = 3) -> None:
        self.client = client
        self.max_symbols = max_symbols
        self.per_side = per_side
        self._flows: dict[str, SymbolFlow] = {}

    def get(self, symbol: str) -> SymbolFlow | None:
        return self._flows.get(symbol.upper())

    def symbols(self) -> list[str]:
        return sorted(self._flows)

    async def start(self, snapshot: ChainSnapshot, contract_for, r: float, q: float) -> SymbolFlow:
        symbol = snapshot.symbol.upper()
        existing = self._flows.get(symbol)
        if existing is not None:
            return existing
        if self.max_symbols <= 0:
            raise ValueError("flow tracking is disabled (GEX_FLOW_MAX_SYMBOLS=0)")
        if len(self._flows) >= self.max_symbols:
            raise ValueError(f"flow tracking limited to {self.max_symbols} symbols")
        self.client.require_connected()
        rows = select_flow_contracts(snapshot, self.per_side)
        if not rows:
            raise ValueError("no live near-ATM contracts to track")
        contracts = [(row, contract_for(row)) for row in rows]
        if self.client.lines_in_use + len(contracts) > self.client.settings.max_md_lines:
            raise ValueError(
                f"flow would need {len(contracts)} lines; "
                f"{self.client.lines_in_use}/{self.client.settings.max_md_lines} already in use"
            )
        flow = SymbolFlow(symbol, snapshot.spot, self.client, contracts, r, q)
        await flow.start()
        self._flows[symbol] = flow
        return flow

    async def stop(self, symbol: str) -> bool:
        flow = self._flows.pop(symbol.upper(), None)
        if flow is None:
            return False
        await flow.stop()
        return True

    async def stop_all(self) -> None:
        for symbol in list(self._flows):
            await self.stop(symbol)

    def preview(self, result: GexResponse) -> WallTape:
        from .gex import front_week_expiry
        from .tape import unavailable_tape

        flow = self.get(result.symbol)
        if flow is not None:
            return flow.wall_tape(result)
        day = result.ts.astimezone(NY).date()
        front = front_week_expiry(result.by_expiry, day)
        expiry = front.expiry if front is not None else None
        call_wall = front.call_wall if front is not None else result.summary.call_wall
        return unavailable_tape(expiry, call_wall)

    async def ensure_wall(self, snapshot: ChainSnapshot, result: GexResponse, contract_for, r: float, q: float) -> SymbolFlow | None:
        """Subscribe the front-week call wall. Retarget when the strike set changes; reset on a new session."""
        from .gex import front_week_expiry
        from .tape import select_wall_rows

        symbol = snapshot.symbol.upper()
        today = snapshot.ts.astimezone(NY).date()
        existing = self._flows.get(symbol)
        if existing is not None and existing.session_day != today:
            await self.stop(symbol)
            existing = None
        front = front_week_expiry(result.by_expiry, today)
        expiry = front.expiry if front is not None else None
        wall = front.call_wall if front is not None else result.summary.call_wall
        rows = select_wall_rows(snapshot, expiry, wall, neighbors=1)
        if not rows:
            return existing
        key = tuple(sorted((row.expiry, row.strike, row.right, row.con_id) for row in rows))
        if existing is not None and existing.key == key:
            existing.update_spot(snapshot.spot)
            return existing
        if existing is not None:
            await self.stop(symbol)
        if self.max_symbols <= 0 or len(self._flows) >= self.max_symbols:
            log.info("wall tape skipped for %s: flow cap is %s", symbol, self.max_symbols)
            return None
        if not hasattr(self.client, "subscribe"):
            return None
        try:
            self.client.require_connected()
        except Exception:  # noqa: BLE001
            return None
        if self.client.lines_in_use + len(rows) > self.client.settings.max_md_lines:
            log.info("wall tape skipped for %s: market-data lines are full", symbol)
            return None
        contracts = [(row, contract_for(row)) for row in rows]
        flow = SymbolFlow(symbol, snapshot.spot, self.client, contracts, r, q)
        flow.session_day = today
        await flow.start()
        self._flows[symbol] = flow
        return flow
