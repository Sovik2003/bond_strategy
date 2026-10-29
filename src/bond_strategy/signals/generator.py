"""Генератор сигналов: фильтр -> доходность -> скоринг -> лимиты -> сигнал."""
from __future__ import annotations

import hashlib
from datetime import date

from ..analytics.scoring import required_premium, total_score
from ..analytics.yield_calc import calculate_ytm
from ..config import StrategyConfig
from ..models import BondIssue, Signal, SignalType
from ..portfolio.engine import (
    LimitViolation,
    Portfolio,
    Prices,
    check_limits,
    position_size,
)
from .filters import hard_filter


def make_signal_id(isin: str, signal_type: SignalType, reason: str, bucket: date) -> str:
    raw = f"{isin}|{signal_type.value}|{reason}|{bucket.isoformat()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def evaluate_issue(
    issue: BondIssue,
    portfolio: Portfolio,
    prices: Prices,
    as_of: date,
    cfg: StrategyConfig,
    commission_rate: float = 0.0,
) -> Signal:
    reasons: list[str] = []
    failed: list[str] = []

    def build(stype: SignalType, score: float, amount: float | None, qty: int | None, note: str) -> Signal:
        bucket = ",".join(failed) or note
        return Signal(
            signal_id=make_signal_id(issue.isin, stype, bucket, as_of),
            isin=issue.isin,
            signal_type=stype,
            created_at=as_of,
            score=score,
            reasons=reasons + [note],
            failed_conditions=list(failed),
            suggested_amount=amount,
            suggested_quantity=qty,
            config_id=cfg.config_id,
        )

    # 1. Жёсткие фильтры
    fr = hard_filter(issue, as_of, cfg)
    if not fr.passed:
        failed.extend(f"{code}: {text}" for code, text in fr.reasons)
        return build(SignalType.DATA_BLOCKED, 0.0, None, None, "Жёсткий фильтр не пройден")

    # 2. Доходность
    yres = calculate_ytm(issue, as_of, commission_rate)
    if not yres.ok or yres.ytm is None:
        failed.append(f"YIELD_UNAVAILABLE: {yres.message}")
        return build(SignalType.WATCH, 0.0, None, None, "Доходность не рассчитана")

    premium = yres.premium_to_ofz
    if premium is None:
        failed.append("NO_OFZ_BENCHMARK: нет сопоставимой ОФЗ")
        return build(SignalType.WATCH, 0.0, None, None, "Нет базы сравнения")

    # 3. Скоринг
    score, _ = total_score(issue, as_of, premium, cfg)
    if score < cfg.min_buy_score:
        failed.append(f"LOW_SCORE: {score:.1f} < {cfg.min_buy_score:.0f}")
        return build(SignalType.WATCH, score, None, None, "Недостаточный балл")

    # 4. Премия к ОФЗ
    req = required_premium(issue, cfg)
    if premium < req:
        failed.append(f"LOW_PREMIUM: {premium:.2%} < {req:.2%}")
        return build(SignalType.WATCH, score, None, None, "Премия ниже порога")

    # 5. Размер позиции
    qty = position_size(portfolio, issue, prices, cfg, commission_rate)
    if qty <= 0:
        failed.append("NO_SIZE: лимиты не позволяют позицию")
        return build(SignalType.WATCH, score, None, None, "Нет допустимого размера")

    # НКД — рубли на бумагу, цена — проценты от номинала. Сложение «в лоб»
    # прибавляло рубли к процентам и завышало стоимость на величину НКД.
    cost = qty * (issue.clean_price / 100.0 * issue.nominal + issue.accrued_interest)
    amount = cost * (1.0 + commission_rate)
    if amount < cfg.min_position:
        failed.append(f"POSITION_TOO_SMALL: {amount:,.0f} ₽ < {cfg.min_position:,.0f} ₽")
        return build(SignalType.WATCH, score, amount, qty, "Позиция меньше минимума")

    # 6. Лимиты
    try:
        check_limits(portfolio, issue, amount, prices, cfg)
    except LimitViolation as exc:
        failed.append(f"{exc.code}: {exc.text}")
        return build(SignalType.WATCH, score, amount, qty, "Нарушение лимитов")

    reasons.append(f"YTM {yres.ytm:.2%}")
    reasons.append(f"Премия к ОФЗ {premium:.2%} (порог {req:.2%})")
    reasons.append(f"Балл {score:.1f}")
    reasons.append(f"Оборот {issue.avg_daily_turnover:,.0f} ₽, спред {issue.avg_spread:.3%}")
    return build(SignalType.BUY, score, amount, qty, "Все условия выполнены")
