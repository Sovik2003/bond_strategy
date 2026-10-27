"""Скоринг выпуска. Жёсткие фильтры он не заменяет."""
from __future__ import annotations

from datetime import date

from ..config import StrategyConfig
from ..models import BondIssue, Quality


def _clamp(v: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, v))


RATING_TABLE = {
    "AAA": 100, "AA+": 96, "AA": 92, "AA-": 88,
    "A+": 84, "A": 80, "A-": 76,
    "BBB+": 68, "BBB": 62, "BBB-": 56,
    "BB+": 44, "BB": 38, "BB-": 32,
}

# Прокси кредитного качества по уровню листинга MOEX.
# Это НЕ кредитный рейтинг: уровень листинга отражает требования биржи
# к раскрытию и качеству эмитента, но не вероятность дефолта.
# Прокси намеренно ограничен сверху (не даёт максимум), чтобы выпуск
# без рейтинга не выглядел лучше выпуска с подтверждённым рейтингом A+.
LISTING_PROXY = {1: 72.0, 2: 66.0, 3: 54.0}


def credit_score(issue: BondIssue) -> float:
    if issue.rating:
        return float(RATING_TABLE.get(issue.rating.upper(), 20))
    if issue.credit_basis == "LISTING_PROXY" and issue.list_level in LISTING_PROXY:
        return LISTING_PROXY[issue.list_level]
    return 0.0


def financial_score(issue: BondIssue) -> float:
    if not issue.reporting_sufficient:
        return 0.0
    if issue.default_flag or issue.restructuring_flag:
        return 0.0

    nd, ic = issue.net_debt_to_ebitda, issue.interest_coverage
    if nd is None and ic is None:
        return 55.0

    parts: list[float] = []
    if nd is not None:
        parts.append(_clamp(100 - 12.5 * max(nd, 0)))
    if ic is not None:
        parts.append(_clamp(20 * (ic - 1) + 20))
    return sum(parts) / len(parts)


def yield_score(issue: BondIssue, premium: float | None, cfg: StrategyConfig) -> float:
    if premium is None:
        return 0.0
    key = "core_premium_min" if issue.quality is Quality.HIGH else "medium_premium_min"
    req = float(cfg.get("signals", key))
    return 0.0 if req <= 0 else _clamp(50 * (premium / req))


def liquidity_score(issue: BondIssue, cfg: StrategyConfig) -> float:
    if issue.avg_daily_turnover is None or issue.avg_spread is None:
        return 0.0
    min_turnover = float(cfg.get("filters", "min_daily_turnover"))
    max_spread = float(
        cfg.get("filters", "max_spread_core" if issue.quality is Quality.HIGH else "max_spread_medium")
    )
    turnover_part = _clamp(50 * issue.avg_daily_turnover / min_turnover) if min_turnover else 0.0
    spread_part = _clamp(100 - 50 * issue.avg_spread / max_spread) if max_spread > 0 else 0.0
    return 0.5 * turnover_part + 0.5 * spread_part


def structure_score(issue: BondIssue, settlement: date, cfg: StrategyConfig) -> float:
    if issue.maturity_date is None:
        return 0.0
    days = (issue.maturity_date - settlement).days
    pref_min = float(cfg.get("filters", "preferred_min_days"))
    pref_max = float(cfg.get("filters", "preferred_max_years")) * 365
    if days < pref_min:
        return 40.0
    if days <= pref_max:
        return 100.0
    return _clamp(100 - 25 * (days - pref_max) / 365.0)


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
