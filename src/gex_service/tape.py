"""Signed customer flow near the open-interest call wall, and the day's path.

A print at or above the ask is a customer buy. The dealer sold that contract, so
dealer gamma from the print is the opposite of the customer's. Enough size on
that tape overrides the open-interest sign for the rest of the session.
"""

from __future__ import annotations

from .chain import ChainRow, ChainSnapshot
from .models import PathSwitch, SessionPath, WallTape

# Below this many classified contracts the tape does not override the open-interest book.
MIN_CLASSIFIED = 20


def select_wall_rows(
    snapshot: ChainSnapshot,
    expiry: str | None,
    call_wall: float | None,
    neighbors: int = 1,
) -> list[ChainRow]:
    """Front-expiry strikes around the call wall, both rights.

    Falls back to the nearest live expiry around spot when the wall is missing.
    """
    live = [row for row in snapshot.rows if row.dte > 0.02]
    if expiry:
        scoped = [row for row in live if row.expiry == expiry]
        if scoped:
            live = scoped
    if not live:
        return []
    if call_wall is None:
        expiry = min(live, key=lambda row: row.dte).expiry
        live = [row for row in live if row.expiry == expiry]
        strikes = sorted({row.strike for row in live})
        below = [strike for strike in strikes if strike <= snapshot.spot][-max(neighbors, 1):]
        above = [strike for strike in strikes if strike > snapshot.spot][:max(neighbors, 1)]
        keep = set(below + above)
        return [row for row in live if row.strike in keep]
    strikes = sorted({row.strike for row in live})
    closest = min(range(len(strikes)), key=lambda i: abs(strikes[i] - call_wall))
    lo = max(0, closest - neighbors)
    hi = min(len(strikes), closest + neighbors + 1)
    keep = set(strikes[lo:hi])
    return [row for row in live if row.strike in keep]


def dealer_gex(net_customer: float, gamma: float, multiplier: float, spot: float) -> float:
    """Positive when customers net sold: dealers are longer gamma."""
    return -net_customer * gamma * multiplier * spot * spot * 0.01


def dealer_shares(net_customer: float, delta: float, multiplier: float) -> float:
    """Shares dealers trade to flatten the new inventory. Positive means they buy."""
    return net_customer * delta * multiplier


def wall_tape_from_legs(
    *,
    expiry: str | None,
    call_wall: float | None,
    strikes: list[float],
    call_buy: float,
    call_sell: float,
    put_buy: float,
    put_sell: float,
    gex: float,
    shares: float,
) -> WallTape:
    classified = call_buy + call_sell + put_buy + put_sell
    if classified < MIN_CLASSIFIED:
        return WallTape(
            expiry=expiry,
            call_wall=call_wall,
            strikes=strikes,
            customer_call_buy=call_buy,
            customer_call_sell=call_sell,
            customer_put_buy=put_buy,
            customer_put_sell=put_sell,
            classified_volume=classified,
            dealer_gex=gex,
            dealer_shares=shares,
            tape="quiet",
            wall_read="unchanged",
            note=(
                f"Classified volume near the call wall is {classified:.0f} contracts. "
                "That is too small to move the open-interest read."
            ),
        )
    if gex < 0:
        return WallTape(
            expiry=expiry,
            call_wall=call_wall,
            strikes=strikes,
            customer_call_buy=call_buy,
            customer_call_sell=call_sell,
            customer_put_buy=put_buy,
            customer_put_sell=put_sell,
            classified_volume=classified,
            dealer_gex=gex,
            dealer_shares=shares,
            tape="shorter_gamma",
            wall_read="fade_weaker",
            note=(
                "Customers are net buyers near the call wall, so dealers are shorter gamma. "
                "Do not fade that open-interest strike on today's tape."
            ),
        )
    return WallTape(
        expiry=expiry,
        call_wall=call_wall,
        strikes=strikes,
        customer_call_buy=call_buy,
        customer_call_sell=call_sell,
        customer_put_buy=put_buy,
        customer_put_sell=put_sell,
        classified_volume=classified,
        dealer_gex=gex,
        dealer_shares=shares,
        tape="longer_gamma",
        wall_read="fade_stands",
        note=(
            "Customers are net sellers near the call wall, so dealers are longer gamma. "
            "A fade into that strike still has today's hedge with it."
        ),
    )


def unavailable_tape(expiry: str | None, call_wall: float | None) -> WallTape:
    return WallTape(
        expiry=expiry,
        call_wall=call_wall,
        tape="unavailable",
        wall_read="unchanged",
        note="The call-wall tape is not on yet. It starts on the next refresh inside regular hours.",
    )


def session_path(path: PathSwitch | None, tape: WallTape | None) -> SessionPath:
    if path is None:
        return SessionPath(source="none", path="flat", note="No expiring book to read.")
    if tape is not None and tape.tape == "shorter_gamma":
        return SessionPath(
            source="tape",
            path="chase",
            note=tape.note,
        )
    if tape is not None and tape.tape == "longer_gamma":
        return SessionPath(
            source="tape",
            path="fade",
            note=tape.note,
        )
    return SessionPath(source="oi", path=path.path, note=path.note)
