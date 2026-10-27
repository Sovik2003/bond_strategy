"""Скоринг выпуска. Жёсткие фильтры он не заменяет — только ранжирует допущенные."""
from __future__ import annotations

from datetime import date

from ..config import StrategyConfig
from ..models import BondIssue, Quality

RATING_SCALE = {
    "AAA": 100, "AA+": 96, "AA": 92, "AA-": 88,
    "A+": 84, "A": 80, "A-": 76,
    "BBB+": 68, "BBB": 62, "BBB-": 56,
    "BB+": 44, "BB": 38, "BB-": 32,
}


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def credit_score(issue: BondIssue) -> float:
    if not issue.rating:
        return 0.0
    return float(RATING_SCALE.get(issue.rating.upper(), 20))


def financial_score(issue: BondIssue) -> float:
    """По метрикам эмитента. Если метрик нет — осторожный прокси, не выше 55."""
    if not issue.reporting_sufficient:
        return 0.0
    if issue.default_flag or issue.restructuring_flag:
        return 0.0

    nd = issue.net_debt_to_ebitda
    ic = issue.interest_coverage
    if nd is None and ic is None:
        return 55.0

    parts: list[float] = []
    if nd is not None:
        # 0x -> 100, 2x -> 75, 4x -> 50, 6x -> 25, >=8x -> 0
        parts.append(_clamp(100 - 12.5 * max(nd, 0.0)))
    if ic is not None:
        # 1x -> 20, 3x -> 60, 5x -> 90, >=6x -> 100
        parts.append(_clamp(20 * (ic - 1) + 20))
    return sum(parts) / len(parts)


def yield_score(issue: BondIssue, premium: float | None, cfg: StrategyConfig) -> float:
    """Порог премии = 50 баллов, двойная премия = 100."""
    if premium is None:
        return 0.0
    required = required_premium(issue, cfg)
    if required <= 0:
        return 0.0
    return _clamp(50 * (premium / required))


def required_premium(issue: BondIssue, cfg: StrategyConfig) -> float:
    key = "core_premium_min" if issue.quality is Quality.HIGH else "medium_premium_min"
    return float(cfg.get("signals", key))


def liquidity_score(issue: BondIssue, cfg: StrategyConfig) -> float:
    """Оборот: порог = 50, двукратный порог = 100. Спред: 0 = 100, порог = 50."""
    if issue.avg_daily_turnover is None or issue.avg_spread is None:
        return 0.0
    min_turnover = float(cfg.get("filters", "min_daily_turnover"))
    max_spread = float(cfg.get("filters", "max_spread_core"))
    if issue.quality is Quality.MEDIUM:
        max_spread = float(cfg.get("filters", "max_spread_medium"))

    turnover_part = _clamp(50 * issue.avg_daily_turnover / min_turnover) if min_turnover else 0.0
    spread_part = _clamp(100 - 50 * issue.avg_spread / max_spread) if max_spread > 0 else 0.0
    return 0.5 * turnover_part + 0.5 * spread_part


def structure_score(issue: BondIssue, settlement: date, cfg: StrategyConfig) -> float:
    """Срок и структура выплат."""
    if issue.maturity_date is None:
        return 0.0
    days = (issue.maturity_date - settlement).days
    max_years = float(cfg.get("filters", "preferred_max_years"))
    if days <= max_years * 365:
        return 100.0
    overrun = days / 365.0 - max_years
    return _clamp(100 - 25 * overrun)


def total_score(
    issue: BondIssue, settlement: date, premium: float | None, cfg: StrategyConfig
) -> tuple[float, dict[str, float]]:
    components = {
        "credit": credit_score(issue),
        "financial": financial_score(issue),
        "yield": yield_score(issue, premium, cfg),
        "liquidity": liquidity_score(issue, cfg),
        "structure": structure_score(issue, settlement, cfg),
    }
    weights = cfg.get("scoring_weights", default={})
    total = sum(components[k] * float(weights.get(k, 0.0)) for k in components)
    return round(total, 2), components
