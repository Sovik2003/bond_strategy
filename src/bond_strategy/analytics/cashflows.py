"""График денежных потоков, XIRR, оценка доходности."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..models import BondIssue

DAYS_IN_YEAR = 365.0


@dataclass(frozen=True)
class CashFlow:
    payment_date: date
    amount_per_bond: float
    kind: str  # coupon | amortization | redemption


def _add_months(d: date, months: int) -> date:
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    day = min(d.day, 28) if month == 2 else d.day
    return date(year, month, day)


def coupon_dates(issue: BondIssue, settlement: date) -> list[date]:
    if issue.maturity_date is None or not issue.coupon_frequency:
        return []
    step = max(12 // issue.coupon_frequency, 1)
    dates: list[date] = []
    current = issue.maturity_date
    while current > settlement:
        dates.append(current)
        current = _add_months(current, -step)
    return sorted(dates)


def outstanding_left_at(issue: BondIssue, target: date) -> float:
    nominal = issue.nominal
    for p in sorted(issue.amortization, key=lambda x: x.payment_date):
        if p.payment_date < target:
            nominal -= issue.nominal * p.share_of_nominal
    return max(nominal, 0.0)


def build_schedule(issue: BondIssue, settlement: date) -> list[CashFlow]:
    """График выплат после даты расчёта.

    Если загружен фактический график купонов из ISS, используется он: ставка у
    реальных выпусков меняется от периода к периоду, поэтому генерация из одной
    константы дала бы неверную доходность. Генерация применяется только
    когда фактического графика нет.
    """
    if issue.maturity_date is None:
        raise ValueError(f"{issue.isin}: не задана дата погашения")

    flows: list[CashFlow] = []
    outstanding = issue.nominal

    # --- купоны
    if issue.coupons:
        for c in issue.coupons:
            if c.payment_date > settlement and c.amount_per_bond > 0:
                flows.append(
                    CashFlow(c.payment_date, round(c.amount_per_bond, 6), "coupon")
                )
    else:
        rate = issue.coupon_rate or 0.0
        fraction = 1.0 / issue.coupon_frequency if issue.coupon_frequency else 0.0
        for cdate in coupon_dates(issue, settlement):
            coupon = outstanding_left_at(issue, cdate) * rate * fraction
            if coupon > 0:
                flows.append(CashFlow(cdate, round(coupon, 6), "coupon"))

    # --- амортизация и погашение
    for p in sorted(issue.amortization, key=lambda x: x.payment_date):
        if p.payment_date <= settlement:
            outstanding -= issue.nominal * p.share_of_nominal
            continue
        amount = issue.nominal * p.share_of_nominal
        flows.append(CashFlow(p.payment_date, round(amount, 6), "amortization"))
        outstanding -= amount

    if outstanding > 1e-9:
        flows.append(CashFlow(issue.maturity_date, round(outstanding, 6), "redemption"))

    return sorted(flows, key=lambda f: f.payment_date)


def uses_actual_coupons(issue: BondIssue) -> bool:
    """True, если график построен по фактическим купонам."""
    return bool(issue.coupons)


def total_cashflow(schedule: list[CashFlow]) -> float:
    return sum(f.amount_per_bond for f in schedule)


def xirr(cashflows: list[tuple[date, float]], guess: float = 0.2) -> float | None:
    if len(cashflows) < 2:
        return None
    t0 = min(d for d, _ in cashflows)
    if not (any(a > 0 for _, a in cashflows) and any(a < 0 for _, a in cashflows)):
        return None

    def npv(rate: float) -> float:
        return sum(
            amount / (1.0 + rate) ** ((d - t0).days / DAYS_IN_YEAR)
            for d, amount in cashflows
        )

    rate = guess
    for _ in range(100):
        f = npv(rate)
        h = 1e-6
        derivative = (npv(rate + h) - f) / h
        if abs(derivative) < 1e-12:
            return None
        new_rate = rate - f / derivative
        if new_rate <= -0.999:
            new_rate = -0.5
        if abs(new_rate - rate) < 1e-9:
            return new_rate
        rate = new_rate
    return None
