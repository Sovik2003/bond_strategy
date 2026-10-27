"""Доходность к погашению по фактическим денежным потокам."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..config import StrategyConfig
from ..models import BondIssue, Quality
from .cashflows import build_schedule, xirr


@dataclass(frozen=True)
class YieldResult:
    ytm: float | None
    premium_to_ofz: float | None
    total_investment: float
    total_payout: float
    ok: bool
    message: str


def purchase_cost_per_bond(issue: BondIssue, commission_rate: float = 0.0) -> float | None:
    clean = issue.clean_price
    nkd = issue.accrued_interest
    if clean is None or nkd is None:
        return None
    return (clean + nkd) / 100.0 * issue.nominal * (1.0 + commission_rate)


def calculate_ytm(
    issue: BondIssue, settlement: date, commission_rate: float = 0.0
) -> YieldResult:
    cost = purchase_cost_per_bond(issue, commission_rate)
    if cost is None:
        return YieldResult(None, None, 0.0, 0.0, False, "Нет цены или НКД")
    if not issue.coupon_rate or issue.coupon_rate <= 0:
        return YieldResult(None, None, 0.0, 0.0, False, "Нет подтверждённой купонной ставки")
    if issue.maturity_date is None or issue.maturity_date <= settlement:
        return YieldResult(None, None, 0.0, 0.0, False, "Некорректная дата погашения")

    try:
        schedule = build_schedule(issue, settlement)
    except ValueError as exc:
        return YieldResult(None, None, 0.0, 0.0, False, str(exc))

    if not schedule:
        return YieldResult(None, None, 0.0, 0.0, False, "Пустой график выплат")

    flows: list[tuple[date, float]] = [(settlement, -cost)]
    flows.extend((f.payment_date, f.amount_per_bond) for f in schedule)

    rate = xirr(flows)
    if rate is None:
        return YieldResult(None, None, cost, 0.0, False, "Доходность не сошлась")

    premium = None if issue.ofz_ytm is None else rate - issue.ofz_ytm
    return YieldResult(
        ytm=rate,
        premium_to_ofz=premium,
        total_investment=cost,
        total_payout=sum(f.amount_per_bond for f in schedule),
        ok=True,
        message="OK",
    )


def required_premium(issue: BondIssue, cfg: StrategyConfig) -> float:
    key = "core_premium_min" if issue.quality is Quality.HIGH else "medium_premium_min"
    return float(cfg.get("signals", key))
