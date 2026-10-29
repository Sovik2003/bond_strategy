"""Дюрация облигации и портфеля.

Дюрация Маколея — средневзвешенный по приведённой стоимости срок поступлений
в годах. Пороги правил («2,5–3 года») заданы именно в этих единицах, поэтому
сравнения идут по Маколею. Модифицированная = Маколей / (1 + y) — множитель
чувствительности цены к ставке, отдаётся в отчёт отдельно.

Дисконтирование то же, что в `cashflows.xirr`: PV = CF / (1 + y)^(дни / 365).
Иначе доходность и дюрация считались бы по разным ставкам.

Дюрация — не предел просадки: при росте кредитного спреда цена уходит дальше.
"""
from __future__ import annotations

from ..models import BondIssue
from .cashflows import build_schedule
from .yield_calc import calculate_ytm

DAYS_IN_YEAR = 365.0


def macaulay_duration(issue: BondIssue, settlement, ytm: float | None = None) -> float | None:
    """Средний срок поступлений в годах. None — если график или ставка неизвестны."""
    if issue.maturity_date is None or issue.maturity_date <= settlement:
        return None
    if ytm is None:
        result = calculate_ytm(issue, settlement)
        if not result.ok or result.ytm is None:
            return None
        ytm = result.ytm
    if ytm <= -1.0:
        return None
    try:
        schedule = build_schedule(issue, settlement)
    except ValueError:
        return None
    if not schedule:
        return None

    total_pv = 0.0
    weighted = 0.0
    for flow in schedule:
        years = (flow.payment_date - settlement).days / DAYS_IN_YEAR
        if years <= 0:
            continue
        pv = flow.amount_per_bond / (1.0 + ytm) ** years
        total_pv += pv
        weighted += pv * years
    if total_pv <= 0:
        return None
    return weighted / total_pv


def modified_duration(issue: BondIssue, settlement, ytm: float | None = None) -> float | None:
    """Модифицированная дюрация: Маколей / (1 + y)."""
    mac = macaulay_duration(issue, settlement, ytm)
    if mac is None:
        return None
    if ytm is None:
        result = calculate_ytm(issue, settlement)
        if not result.ok or result.ytm is None:
            return None
        ytm = result.ytm
    return mac / (1.0 + ytm)


def portfolio_duration(weights: dict[str, tuple[float | None, float]]) -> float | None:
    """Взвешенная дюрация по покрытой части.

    `weights`: {isin: (дюрация или None, доля портфеля)}. Бумаги без дюрации
    не обнуляют результат, но их доля видна через `duration_coverage`.
    """
    covered = [(d, w) for d, w in weights.values() if d is not None and w > 0]
    if not covered:
        return None
    total = sum(w for _, w in covered)
    if total <= 0:
        return None
    return sum(d * w for d, w in covered) / total


def duration_coverage(weights: dict[str, tuple[float | None, float]]) -> float:
    """Доля портфеля, для которой дюрация вообще посчитана."""
    total = sum(w for _, w in weights.values() if w > 0)
    if total <= 0:
        return 0.0
    covered = sum(w for d, w in weights.values() if d is not None and w > 0)
    return covered / total
