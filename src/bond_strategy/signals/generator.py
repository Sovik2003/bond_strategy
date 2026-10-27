"""Генератор сигналов."""
from __future__ import annotations

import hashlib
from datetime import date

from ..analytics.scoring import total_score
from ..analytics.yield_calc import calculate_ytm, required_premium
from ..config import StrategyConfig
from ..models import BondIssue, Quality, Signal, SignalType
from ..portfolio.engine import LimitViolation, Portfolio, check_limits, position_size
from .filters import hard_filter


def make_signal_id(isin: str, signal_type: SignalType, reason: str, bucket: date) -> str:
    raw = f"{isin}|{signal_type.value}|{reason}|{bucket.isoformat()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def evaluate_issue(
    issue: BondIssue,
    portfolio: Portfolio,
    prices: dict[str, tuple[float, float]],
    settlement: date,
    cfg: StrategyConfig,
    commission_rate: float = 0.0,
) -> Signal:
    reasons: list[str] = []
    failed: list[str] = []

    def build(stype: SignalType, score: float, amount: float | None, note: str) -> Signal:
        rid = make_signal_id(issue.isin, stype, ",".join(failed) or note, settlement)
        return Signal(
            signal_id=rid,
            isin=issue.isin,
            signal_type=stype,
            created_at=settlement,
            score=score,
            reasons=reasons + [note],
            failed_conditions=failed,
            suggested_amount=amount,
            config_id=cfg.config_id,
        )

    fr = hard_filter(issue, settlement, cfg)
    if not fr.passed:
        for code, text in fr.reasons:
            failed.append(f"{code}: {text}")
        return build(SignalType.DATA_BLOCKED, 0.0, None, "Жёсткий фильтр не пройден")

    yres = calculate_ytm(issue, settlement, commission_rate)
    if not yres.ok or yres.ytm is None:
        failed.append(f"YIELD_UNAVAILABLE: {yres.message}")
        return build(SignalType.WATCH, 0.0, None, "Доходность не рассчитана")

    issue.ytm = yres.ytm
    premium = yres.premium_to_ofz
    if premium is None:
        failed.append("NO_OFZ_BENCHMARK: нет сопоставимой доходности ОФЗ")
        return build(SignalType.WATCH, 0.0, None, "Нет базы для сравнения")

    req = required_premium(issue, cfg)
    score, _ = total_score(issue, settlement, premium, cfg)

    if score < cfg.min_buy_score:
        failed.append(f"LOW_SCORE: {score:.1f} < {cfg.min_buy_score:.0f}")
        return build(SignalType.WATCH, score, None, "Недостаточный итоговый балл")

    if premium < req:
        failed.append(f"LOW_PREMIUM: {premium:.2%} < {req:.2%}")
        return build(SignalType.WATCH, score, None, "Премия к ОФЗ ниже порога")

    qty = position_size(portfolio, issue, prices, cfg, commission_rate)
    if qty <= 0:
        failed.append("NO_SIZE: позиция не проходит лимиты")
        return build(SignalType.WATCH, score, None, "Нет допустимого размера позиции")

    dirty = issue.dirty_price() or 0.0
    amount = qty * dirty / 100.0 * issue.nominal * (1 + commission_rate)

    if amount < cfg.min_position:
        failed.append(f"POSITION_TOO_SMALL: {amount:,.0f} ₽ < {cfg.min_position:,.0f} ₽")
        return build(SignalType.WATCH, score, amount, "Позиция меньше минимума")

    if issue.quality is Quality.MEDIUM and portfolio.medium_quality_share(prices) >= float(
        cfg.get("limits", "medium_quality_max_share")
    ):
        failed.append("MEDIUM_LIMIT: лимит среднего качества исчерпан")
        return build(SignalType.WATCH, score, amount, "Лимит среднего качества")

    try:
        check_limits(portfolio, issue, amount, prices, cfg)
    except LimitViolation as exc:
        failed.append(f"{exc.code}: {exc.text}")
        return build(SignalType.WATCH, score, amount, "Нарушение лимитов")

    reasons.append(f"Доходность к погашению {yres.ytm:.2%}")
    reasons.append(f"Премия к ОФЗ {premium:.2%} (порог {req:.2%})")
    if issue.avg_daily_turnover and issue.avg_spread is not None:
        reasons.append(
            f"Оборот {issue.avg_daily_turnover:,.0f} ₽, спред {issue.avg_spread:.3%}"
        )
    return build(SignalType.BUY, score, amount, "Все условия выполнены")
