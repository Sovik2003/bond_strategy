"""Жёсткие фильтры допуска выпуска к анализу.

Важное отличие реального контура от тестового: MOEX ISS не отдаёт
кредитный рейтинг. Поэтому отсутствие рейтинга НЕ означает «нет данных» —
оно означает, что оценка качества опирается на уровень листинга.
Это фиксируется отдельным статусом, а не подменяется выдуманным рейтингом.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..config import StrategyConfig
from ..models import BondIssue, DataStatus, Quality

# Поля, отсутствие которых в ISS ожидаемо и не блокирует анализ.
RATING_PLACEHOLDER = "UNVERIFIED"

# Поля, отсутствие которых в ISS ожидаемо и не блокирует анализ.
#   rating   — ISS кредитных рейтингов не отдаёт;
#   industry — ISS отраслей не отдаёт.
# Отсутствие этих полей Limit-проверками учитывается отдельно: лимит по
# отрасли в таком случае не применяется, а качество оценивается по прокси.
# Всё остальное (цена, НКД, купон, погашение, доступность, дефолт, ликвидность)
# по-прежнему блокирует покупку.
NON_BLOCKING_FIELDS = {"rating", "industry"}


@dataclass
class FilterResult:
    passed: bool
    reasons: list[tuple[str, str]] = field(default_factory=list)

    def fail(self, code: str, text: str) -> "FilterResult":
        self.passed = False
        self.reasons.append((code, text))
        return self


def hard_filter(issue: BondIssue, settlement: date, cfg: StrategyConfig) -> FilterResult:
    result = FilterResult(passed=True)

    # --- критические данные (кроме рейтинга — он отсутствует в ISS по определению)
    critical_blockers = {
        name: st
        for name, st in issue.status.items()
        if st.blocks_buy and name not in NON_BLOCKING_FIELDS
    }
    if critical_blockers:
        worst = _worst(critical_blockers.values())
        result.fail(
            f"DATA_BLOCKED_{worst.value}",
            "Критические данные не подтверждены: "
            + ", ".join(sorted(critical_blockers)),
        )

    if issue.default_flag:
        result.fail("EXCLUDED_DEFAULT", "Подтверждён дефолт")
    if issue.restructuring_flag:
        result.fail("EXCLUDED_RESTRUCTURING", "Объявлена реструктуризация")
    if issue.offer_flag:
        result.fail("EXCLUDED_OFFER", "Есть будущая оферта")
    if issue.coupon_type != "FIXED":
        result.fail("EXCLUDED_FLOATING_COUPON", "Не фиксированный купон")
    if issue.qualified_only:
        result.fail("EXCLUDED_QUALIFIED_ONLY", "Только для квалифицированных инвесторов")
    if not issue.reporting_sufficient:
        result.fail("EXCLUDED_REPORTING", "Недостаточное раскрытие")

    if issue.maturity_date is None:
        result.fail("EXCLUDED_NO_MATURITY", "Не указана дата погашения")
    else:
        days = (issue.maturity_date - settlement).days
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


def _worst(values) -> DataStatus:
    order = [DataStatus.CONFLICT, DataStatus.MISSING, DataStatus.UNVERIFIED, DataStatus.STALE]
    present = set(values)
    for level in order:
        if level in present:
            return level
    return DataStatus.FRESH
