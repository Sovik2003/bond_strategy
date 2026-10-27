"""Ликвидность выпуска: средний дневной оборот из истории и текущий спред.

ISS не отдаёт готовый «средний оборот», поэтому он считается по истории
торгов. Спред берётся из текущего стакана: в истории bid/offer нет.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .client import IssClient, block, num

HISTORY_PATH = "/history/engines/stock/markets/bonds/securities/{secid}.json"
BOARD_PATH = "/engines/stock/markets/bonds/boards/{board}/securities.json"


@dataclass
class LiquidityStats:
    secid: str
    days_observed: int
    days_with_trades: int
    avg_daily_turnover: float | None
    median_daily_turnover: float | None
    avg_trades_per_day: float | None
    last_trade_date: date | None
    bid: float | None
    offer: float | None
    spread_pct: float | None
    ok: bool
    message: str


def fetch_history(
    client: IssClient,
    secid: str,
    as_of: date,
    lookback_days: int = 30,
    stamp: str | None = None,
) -> list[dict]:
    """История торгов за последние lookback_days календарных дней."""
    start = as_of - timedelta(days=lookback_days)
    payload = client.get(
        HISTORY_PATH.format(secid=secid),
        stamp=stamp,
        **{"from": start.isoformat(), "till": as_of.isoformat()},
    )
    return block(payload, "history")


def compute_liquidity(
    client: IssClient,
    secid: str,
    as_of: date,
    board: str = "TQCB",
    lookback_days: int = 30,
    stamp: str | None = None,
) -> LiquidityStats:
    try:
        rows = fetch_history(client, secid, as_of, lookback_days, stamp)
    except Exception as exc:  # сеть/404 -> нет данных, но не падение
        return LiquidityStats(
            secid, 0, 0, None, None, None, None, None, None, None, False, f"HISTORY_ERROR:{exc}"
        )

    if not rows:
        return LiquidityStats(
            secid, 0, 0, None, None, None, None, None, None, None, False, "NO_HISTORY"
        )

    values = [
        v for r in rows
        if (v := num(r.get("VALUE"))) is not None and v > 0
    ]
    trades = [
        t for r in rows
        if (t := num(r.get("NUMTRADES"))) is not None and t > 0
    ]

    avg = sum(values) / len(values) if values else None
    median = sorted(values)[len(values) // 2] if values else None

    last_dates = sorted(
        (r.get("TRADEDATE") for r in rows if num(r.get("VALUE"))), reverse=True
    )
    last_trade_date = date.fromisoformat(last_dates[0][:10]) if last_dates else None

    bid = offer = spread = None
    try:
        payload = client.get(
            BOARD_PATH.format(board=board),
            stamp=stamp,
            **{"iss.only": "marketdata", "securities": secid},
        )
        md = block(payload, "marketdata")
        if md:
            bid = num(md[0].get("BID"))
            offer = num(md[0].get("OFFER"))
            if bid and offer and bid > 0 and offer > 0:
                spread = (offer - bid) / ((offer + bid) / 2)
    except Exception:
        pass

    return LiquidityStats(
        secid=secid,
        days_observed=len(rows),
        days_with_trades=len(values),
        avg_daily_turnover=avg,
        median_daily_turnover=median,
        avg_trades_per_day=(sum(trades) / len(trades)) if trades else None,
        last_trade_date=last_trade_date,
        bid=bid,
        offer=offer,
        spread_pct=spread,
        ok=avg is not None,
        message="OK" if avg is not None else "НЕТ СДЕЛОК ЗА ПЕРИОД",
    )
