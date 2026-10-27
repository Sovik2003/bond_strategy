"""Доходность к погашению с учётом цены, НКД и комиссии."""
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
    cost_per_bond: float
    total_payout_per_bond: float
    ok: bool
    message: str


def purchase_cost_per_bond(issue: BondIssue, commission_rate: float = 0.0) -> float | None:
    """Полная стоимость одной бумаги: грязная цена плюс комиссия."""
    if issue.clean_price is None or issue.accrued_interest is None:
        return None
    gross = (issue.clean_price + issue.accrued_interest) / 100.0 * issue.nominal
    return gross * (1.0 + commission_rate)


def calculate_ytm(
    issue: BondIssue, settlement: date, commission_rate: float = 0.0
) -> YieldResult:
    """Доходность по фактическим потокам. При нехватке данных — ok=False."""
    cost = purchase_cost_per_bond(issue, commission_rate)
    if cost is None:
        return YieldResult(None, None, 0.0, 0.0, False, "Нет цены или НКД")
    if not issue.coupon_schedule and not issue.coupon_rate:
        return YieldResult(None, None, cost, 0.0, False, "Нет подтверждённой купонной ставки")
    if issue.maturity_date is None or issue.maturity_date <= settlement:
        return YieldResult(None, None, cost, 0.0, False, "Некорректная дата погашения")

    try:
        schedule = build_schedule(issue, settlement)
    except ValueError as exc:
        return YieldResult(None, None, cost, 0.0, False, str(exc))

    if not schedule:
        return YieldResult(None, None, cost, 0.0, False, "Пустой график выплат")

    flows: list[tuple[date, float]] = [(settlement, -cost)]
    flows.extend((f.payment_date, f.amount_per_bond) for f in schedule)

    rate = xirr(flows)
    if rate is None:
        return YieldResult(None, None, cost, 0.0, False, "Доходность не сошлась")

    payout = sum(f.amount_per_bond for f in schedule)
    premium = rate - issue.ofz_ytm if issue.ofz_ytm is not None else None
    return YieldResult(rate, premium, cost, payout, True, "OK")


def required_premium(issue: BondIssue, cfg: StrategyConfig) -> float:
    key = "core_premium_min" if issue.quality is Quality.HIGH else "medium_premium_min"
    return float(cfg.get("signals", key))
