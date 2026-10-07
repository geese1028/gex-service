"""Trailing realized-vol echo for index products.

Target-vol funds scale equity exposure to a realized-vol window. A session that
jumps that window keeps them adjusting for several sessions after the print,
because the window still contains it. A long quiet stretch rebuilds exposure
and leaves the next shock with more to unwind. The AUM is not observed here.
Single names do not get this book.
"""

from __future__ import annotations

from datetime import date
from math import sqrt
from statistics import pstdev

from .models import VolControl

INDEX_PRODUCTS = frozenset({"SPY", "QQQ", "IWM", "DIA", "SPX", "NDX", "RUT", "XSP", "OEX", "MDY"})
LOOKBACK = 20
SHOCK_SIGMA = 2.0
ECHO_SESSIONS = 5
QUIET_RATIO = 0.8


def index_book(symbol: str, sec_type: str = "") -> bool:
    return symbol.upper() in INDEX_PRODUCTS or (sec_type == "IND" and symbol.upper() in INDEX_PRODUCTS)


def _off(symbol: str) -> VolControl:
    return VolControl(
        applies=False,
        state="off",
        note=f"{symbol.upper()} is not an index product. Volatility-target funds do not leave an echo on this name.",
    )


def _unavailable(reason: str) -> VolControl:
    return VolControl(applies=True, state="unavailable", note=reason)


def vol_control_from_closes(
    symbol: str,
    closes: list[tuple[date, float]],
    *,
    as_of: date,
    sec_type: str = "",
) -> VolControl:
    if not index_book(symbol, sec_type):
        return _off(symbol)
    ordered = [(day, close) for day, close in closes if day < as_of and close > 0]
    ordered.sort(key=lambda item: item[0])
    if len(ordered) < LOOKBACK + 1:
        return _unavailable("Not enough completed sessions to measure the trailing window.")
    returns = [
        (ordered[i][0], ordered[i][1] / ordered[i - 1][1] - 1.0)
        for i in range(1, len(ordered))
    ]
    window = [ret for _, ret in returns[-LOOKBACK:]]
    sigma_20 = pstdev(window)
    rv_20 = sigma_20 * sqrt(252.0)
    last_return = returns[-1][1]
    shock: tuple[date, float] | None = None
    for i in range(LOOKBACK, len(returns)):
        prior = [returns[j][1] for j in range(i - LOOKBACK, i)]
        sigma = pstdev(prior)
        if sigma > 0 and abs(returns[i][1]) >= SHOCK_SIGMA * sigma:
            shock = returns[i]
    if shock is not None:
        shock_day, shock_ret = shock
        since = sum(1 for day, _ in returns if day > shock_day)
        left = ECHO_SESSIONS - since
        if left > 0:
            return VolControl(
                applies=True,
                state="selling",
                rv_20d=rv_20,
                last_return=last_return,
                shock_date=shock_day.isoformat(),
                shock_return=shock_ret,
                sessions_since_shock=since,
                echo_left=left,
                note=(
                    f"The session of {shock_day.isoformat()} moved {shock_ret:.1%}, "
                    f"more than {SHOCK_SIGMA:.0f} trailing standard deviations. "
                    f"Target-vol funds keep adjusting while that day stays in the window. "
                    f"About {left} session(s) of that echo are left. This is not a measured fund flow."
                ),
            )
    rv_10 = pstdev([ret for _, ret in returns[-10:]]) * sqrt(252.0) if len(returns) >= 10 else None
    if rv_10 is not None and rv_20 > 0 and rv_10 < QUIET_RATIO * rv_20:
        return VolControl(
            applies=True,
            state="full",
            rv_20d=rv_20,
            last_return=last_return,
            note=(
                "Realized vol has compressed against its 20-session window, so target-vol exposure "
                "is rebuilt. The next shock has more to unwind. This is not a measured fund flow."
            ),
        )
    return VolControl(
        applies=True,
        state="quiet",
        rv_20d=rv_20,
        last_return=last_return,
        note="No shock is still inside the echo window, and realized vol is not in a quiet trough.",
    )
