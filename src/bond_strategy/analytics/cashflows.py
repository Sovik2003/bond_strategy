"""Денежные потоки по облигации.

Два источника графика:
1. Фактический график купонов из ISS (bondization) — приоритетный;
2. Расчёт по ставке — только если фактического графика нет.

Купоны начисляются на остаточный номинал: после амортизации купон меньше.
"""
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
    total = d.month - 1 + months
    year = d.year + total // 12
    month = total % 12 + 1
    day = min(d.day, 28) if month == 2 else d.day
    return date(year, month, day)


def synthetic_coupon_dates(issue: BondIssue, settlement: date) -> list[date]:
    """Купонные даты от даты расчёта до погашения — от погашения назад шагом периода."""
    if issue.maturity_date is None or not issue.coupon_frequency:
        return []
    step = 12 // issue.coupon_frequency
    dates: list[date] = []
    current = issue.maturity_date
    while current > settlement:
        dates.append(current)
        current = _add_months(current, -step)
    return sorted(dates)


def outstanding_before(issue: BondIssue, target: date) -> float:
    """Остаточный номинал на дату: амортизация строго до неё уже выплачена."""
    nominal = issue.initial_nominal
    for p in sorted(issue.amortization, key=lambda x: x.payment_date):
        if p.payment_date < target:
            nominal -= issue.initial_nominal * p.share_of_nominal
    return max(nominal, 0.0)


# Допуск на расхождение дат между расчётным графиком и фактическим. ISS
# ставит купон на дату выплаты, а расчёт идёт от даты погашения шагом периода:
# расхождение в несколько дней — норма, а не другой график.
DATE_TOLERANCE_DAYS = 10


def _matches(pay_date: date, expected: list[date]) -> bool:
    return any(abs((pay_date - candidate).days) <= DATE_TOLERANCE_DAYS for candidate in expected)


def _coupon_flows(issue: BondIssue, settlement: date) -> list[tuple[date, float]]:
    """Купоны после даты расчёта: суммы из графика, недостающие — по ставке.

    Возвращает пары (дата, сумма на бумагу).
    """
    scheduled = [
        (pay_date, amount)
        for pay_date, amount in sorted(issue.coupon_schedule)
        if pay_date > settlement and amount > 0
    ]
    if not issue.coupon_rate:
        return scheduled

    fraction = 1.0 / issue.coupon_frequency if issue.coupon_frequency else 0.0
    expected = synthetic_coupon_dates(issue, settlement)
    if not expected:
        return scheduled

    # График не про те же даты — значит он описывает собственный график выпуска,
    # и подменять его расчётом нельзя.
    if scheduled and not any(_matches(pay_date, expected) for pay_date, _ in scheduled):
        return scheduled

    known = {}
    for pay_date, amount in scheduled:
        nearest = min(expected, key=lambda candidate: abs((pay_date - candidate).days))
        if abs((nearest - pay_date).days) <= DATE_TOLERANCE_DAYS:
            known[nearest] = amount

    out: list[tuple[date, float]] = []
    for pay_date in expected:
        if pay_date in known:
            out.append((pay_date, known[pay_date]))
            continue
        coupon = outstanding_before(issue, pay_date) * issue.coupon_rate * fraction
        if coupon > 0:
            out.append((pay_date, coupon))
    return out


def build_schedule(issue: BondIssue, settlement: date) -> list[CashFlow]:
    """Полный график выплат строго после даты расчёта.

    Остаток номинала гасится один раз: амортизационные платежи и финальное
    погашение не могут начислить один и тот же номинал дважды.
    """
    if issue.maturity_date is None:
        raise ValueError(f"{issue.isin}: не задана дата погашения")

    flows: list[CashFlow] = []

    # Приоритет фактического графика не безусловен. MOEX ISS отдаёт ДАТЫ всех
    # будущих купонов, но суммы у них часто пустые (`value: null`): известны
    # только уже выплаченные. Из такого графика в `coupon_schedule` попадает
    # одна-две записи на весь срок, и если доверять ему целиком, доходность
    # считается по купонам, которых не будет. Именно так получалась
    # отрицательная доходность у бумаг с купоном 16% годовых.
    #
    # Различие по признаку, а не по количеству. Если даты графика совпадают с
    # расчётными, график описывает те же купоны, и недостающие суммы
    # восстанавливаются по ставке. Если даты другие — у выпуска свой,
    # нестандартный график, и фактические данные главнее расчёта.
    for pay_date, amount in _coupon_flows(issue, settlement):
        flows.append(CashFlow(pay_date, round(amount, 6), "coupon"))

    # Амортизация списывает номинал от исходного значения.
    # Погашение возвращает остаток текущего номинала: часть могла быть
    # выплачена амортизацией ещё до даты расчёта.
    consumed = sum(
        issue.initial_nominal * p.share_of_nominal
        for p in issue.amortization
        if p.payment_date > settlement
    )
    for p in sorted(issue.amortization, key=lambda x: x.payment_date):
        if p.payment_date > settlement:
            amount = issue.initial_nominal * p.share_of_nominal
            flows.append(CashFlow(p.payment_date, round(amount, 6), "amortization"))

    remaining = max(issue.nominal - consumed, 0.0)
    if remaining > 1e-9:
        flows.append(CashFlow(issue.maturity_date, round(remaining, 6), "redemption"))

    return sorted(flows, key=lambda f: f.payment_date)


def total_payout(schedule: list[CashFlow]) -> float:
    return sum(f.amount_per_bond for f in schedule)


def xirr(cashflows: list[tuple[date, float]], guess: float = 0.2) -> float | None:
    """Внутренняя ставка доходности. None, если не сошлось — не ноль."""
    if len(cashflows) < 2:
        return None
    if not (any(a > 0 for _, a in cashflows) and any(a < 0 for _, a in cashflows)):
        return None

    t0 = min(d for d, _ in cashflows)

    def npv(rate: float) -> float:
        return sum(
            amount / (1.0 + rate) ** ((d - t0).days / DAYS_IN_YEAR)
            for d, amount in cashflows
        )

    rate = guess
    for _ in range(200):
        f = npv(rate)
        h = 1e-6
        derivative = (npv(rate + h) - f) / h
        if abs(derivative) < 1e-12:
            return None
        new_rate = rate - f / derivative
        if new_rate <= -0.999:
            new_rate = -0.5
        if abs(new_rate - rate) < 1e-10:
            return new_rate
        rate = new_rate
    return None
