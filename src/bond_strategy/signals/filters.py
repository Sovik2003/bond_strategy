"""Жёсткие фильтры допуска. Каждый отказ имеет код и текст причины."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..config import StrategyConfig
from ..models import BondIssue, DataStatus, Quality


@dataclass
class FilterResult:
    passed: bool = True
    reasons: list[tuple[str, str]] = field(default_factory=list)

    def fail(self, code: str, text: str) -> "FilterResult":
        self.passed = False
        self.reasons.append((code, text))
        return self

    @property
    def codes(self) -> set[str]:
        return {code for code, _ in self.reasons}


def hard_filter(issue: BondIssue, as_of: date, cfg: StrategyConfig) -> FilterResult:
    """Любое нарушение — отказ. Проверки идут от данных к свойствам выпуска."""
    result = FilterResult()

    worst = issue.critical_status()
    if worst is not DataStatus.FRESH:
        blocked = ", ".join(issue.blocked_fields()) or "нет полей"
        result.fail(f"DATA_BLOCKED_{worst.value}", f"Данные не подтверждены: {blocked}")

    if issue.default_flag:
        result.fail("EXCLUDED_DEFAULT", "Подтверждён дефолт")
    if issue.restructuring_flag:
        result.fail("EXCLUDED_RESTRUCTURING", "Объявлена реструктуризация")
    if issue.offer_date is not None and issue.offer_date > as_of:
        result.fail("EXCLUDED_OFFER", f"Оферта {issue.offer_date.isoformat()}")
    if issue.coupon_type != "FIXED":
        result.fail("EXCLUDED_COUPON_TYPE", f"Тип купона: {issue.coupon_type}")
    if issue.qualified_only:
        result.fail("EXCLUDED_QUALIFIED_ONLY", "Только для квалифицированных инвесторов")
    if issue.rating is None:
        result.fail("EXCLUDED_NO_RATING", "Нет рейтинга в справочнике")

    max_level = int(cfg.get("filters", "max_list_level"))
    if issue.list_level is not None and issue.list_level > max_level:
        result.fail("EXCLUDED_LIST_LEVEL", f"Уровень листинга {issue.list_level} > {max_level}")

    if issue.maturity_date is None:
        result.fail("EXCLUDED_NO_MATURITY", "Нет даты погашения")
    else:
        days = (issue.maturity_date - as_of).days
        min_days = int(cfg.get("filters", "min_days_to_maturity"))
        if days < min_days:
            result.fail("EXCLUDED_TOO_SHORT", f"До погашения {days} дн. < {min_days}")

    if issue.avg_daily_turnover is None:
        result.fail("EXCLUDED_NO_TURNOVER", "Нет данных об обороте")
    else:
        min_turnover = float(cfg.get("filters", "min_daily_turnover"))
        if issue.avg_daily_turnover < min_turnover:
            result.fail(
                "EXCLUDED_LOW_LIQUIDITY",
                f"Оборот {issue.avg_daily_turnover:,.0f} ₽ < {min_turnover:,.0f} ₽",
            )

    if issue.avg_spread is None:
        result.fail("EXCLUDED_NO_SPREAD", "Нет данных о спреде")
    else:
        key = "max_spread_core" if issue.quality is Quality.HIGH else "max_spread_medium"
        max_spread = float(cfg.get("filters", key))
        if issue.avg_spread > max_spread:
            result.fail("EXCLUDED_WIDE_SPREAD", f"Спред {issue.avg_spread:.3%} > {max_spread:.3%}")

    return result
