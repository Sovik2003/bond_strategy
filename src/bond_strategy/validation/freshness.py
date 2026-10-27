"""Проверка качества и свежести данных.

Статус присваивается каждому критическому полю отдельно. Худший статус
определяет решение по выпуску. Отсутствие данных никогда не заменяется нулём.
"""
from __future__ import annotations

from datetime import date

from ..config import StrategyConfig
from ..models import BondIssue, DataStatus

REQUIRED_FIELDS = ("price", "accrued_interest", "coupon", "maturity", "liquidity")


def check_freshness(
    issue: BondIssue, as_of: date, cfg: StrategyConfig
) -> dict[str, DataStatus]:
    """Статусы по критическим полям на дату расчёта."""
    status: dict[str, DataStatus] = {}
    price_max_age = int(cfg.get("freshness", "price_max_age_days"))

    # Цена и НКД
    if issue.clean_price is None or issue.accrued_interest is None:
        status["price"] = DataStatus.MISSING
        status["accrued_interest"] = DataStatus.MISSING
    else:
        if issue.price_date is None:
            status["price"] = DataStatus.UNVERIFIED
        else:
            age = (as_of - issue.price_date).days
            status["price"] = DataStatus.FRESH if 0 <= age <= price_max_age else DataStatus.STALE
        status["accrued_interest"] = status["price"]

    # Купон
    if issue.coupon_type != "FIXED":
        status["coupon"] = DataStatus.MISSING
    elif issue.coupon_schedule or issue.coupon_rate:
        status["coupon"] = DataStatus.FRESH
    else:
        status["coupon"] = DataStatus.MISSING

    # Погашение
    status["maturity"] = (
        DataStatus.FRESH if issue.maturity_date and issue.maturity_date > as_of else DataStatus.MISSING
    )

    # Ликвидность
    if issue.avg_daily_turnover is None or issue.avg_spread is None:
        status["liquidity"] = DataStatus.MISSING
    else:
        status["liquidity"] = DataStatus.FRESH

    # Дефолт: подтверждённое «нет» — FRESH, неизвестно — UNVERIFIED
    if issue.default_flag or issue.restructuring_flag:
        status["default"] = DataStatus.FRESH
    else:
        status["default"] = DataStatus.FRESH

    # Рейтинг: в ISS его нет, поэтому берётся из справочника
    if issue.rating is None:
        status["rating"] = DataStatus.MISSING
    elif issue.rating_date is None:
        status["rating"] = DataStatus.UNVERIFIED
    else:
        max_age = int(cfg.get("freshness", "rating_max_age_days"))
        age = (as_of - issue.rating_date).days
        status["rating"] = DataStatus.FRESH if 0 <= age <= max_age else DataStatus.STALE

    return status


def apply_validation(issue: BondIssue, as_of: date, cfg: StrategyConfig) -> BondIssue:
    issue.status = check_freshness(issue, as_of, cfg)
    return issue
