"""Статусы действующих позиций — шаг 2 раздела 5.

Каждой позиции присваивается статус. Позиция может одновременно иметь
«держать» и «не докупать» — это не ошибка, а нормальное состояние: риск не
ухудшился, но лимит уже исчерпан, поэтому новую покупку добавлять нельзя.

Главное правило раздела: продажа только при ухудшении риска. Всё остальное —
снижение цены, рост ставок, выход доли за лимит из-за изменения цены,
отклонение от целевой структуры сроков — поводы не продавать, а корректировать
структуру будущими покупками. Поэтому рядом со статусом в отчёт идёт список
того, что причиной продажи не является: без него инвестор увидит просадку по
бумаге и решит, что её надо продавать.

«Рассмотреть продажу» — рекомендация, а не сделка. После дефолта бумагу может
быть невозможно продать по приемлемой цене, и алгоритм это не скрывает.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum

from ..analytics.duration import macaulay_duration
from ..analytics.yield_calc import calculate_ytm
from .config import RebalanceConfig
from .eligibility import check_eligibility, check_risk_quality
from .limits import check_purchase, violations
from .snapshot import Holding, PortfolioState


class PositionStatus(str, Enum):
    HOLD = "HOLD"
    DO_NOT_ADD = "DO_NOT_ADD"
    CHECK_RISK = "CHECK_RISK"
    CONSIDER_SELL = "CONSIDER_SELL"

    @property
    def label(self) -> str:
        return {
            "HOLD": "Держать",
            "DO_NOT_ADD": "Не докупать",
            "CHECK_RISK": "Проверить риск",
            "CONSIDER_SELL": "Рассмотреть продажу",
        }[self.value]


# Причины, которых недостаточно для рекомендации продажи. Список нужен в
# отчёте: инвестор должен видеть, почему выросшая доля не привела к продаже.
NOT_A_SELL_REASON = (
    "Снижение рыночной цены, рост ставок, выход доли выпуска или отрасли за "
    "лимит только из-за изменения цены и отклонение от целевой структуры сроков "
    "не являются причинами продажи."
)


@dataclass
class PositionAssessment:
    isin: str
    secid: str
    security_name: str | None
    issuer_name: str
    industry: str
    quantity: int
    value: float
    share: float
    rating: str | None
    years_to_maturity: float | None
    band_code: str | None
    duration: float | None
    ytm: float | None
    statuses: list[PositionStatus] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    limit_notes: list[str] = field(default_factory=list)

    @property
    def status(self) -> PositionStatus:
        """Главный статус для отчёта.

        Берётся худший из присвоенных. Если позиция одновременно «держать» и
        «не докупать», показывается «не докупать» — иначе инвестор прочитает
        «держать» как разрешение купить ещё.
        """
        for candidate in (
            PositionStatus.CONSIDER_SELL,
            PositionStatus.CHECK_RISK,
            PositionStatus.DO_NOT_ADD,
            PositionStatus.HOLD,
        ):
            if candidate in self.statuses:
                return candidate
        return PositionStatus.HOLD

    @property
    def needs_sell(self) -> bool:
        return PositionStatus.CONSIDER_SELL in self.statuses

    @property
    def can_add(self) -> bool:
        """Можно ли докупать эту же бумагу.

        «Проверить риск» само по себе не запрещает докупку: запрет возникает
        только из явных причин — лимита или несоответствия разделу 3.
        """
        return PositionStatus.DO_NOT_ADD not in self.statuses and not self.needs_sell

    def as_dict(self) -> dict:
        return {
            "isin": self.isin,
            "secid": self.secid,
            "security_name": self.security_name,
            "issuer": self.issuer_name,
            "industry": self.industry,
            "quantity": self.quantity,
            "value": round(self.value, 2),
            "share": round(self.share, 6),
            "rating": self.rating,
            "years_to_maturity": (
                None if self.years_to_maturity is None else round(self.years_to_maturity, 3)
            ),
            "band": self.band_code,
            "duration": None if self.duration is None else round(self.duration, 4),
            "ytm": None if self.ytm is None else round(self.ytm, 6),
            "status": self.status.value,
            "status_label": self.status.label,
            "statuses": [s.value for s in self.statuses],
            "reasons": self.reasons,
            "warnings": self.warnings,
            "limit_notes": self.limit_notes,
            "can_add": self.can_add,
        }


def assess_position(
    holding: Holding,
    state: PortfolioState,
    cfg: RebalanceConfig,
    as_of: date,
) -> PositionAssessment:
    """Статусы одной позиции."""
    issue = holding.issue
    years = state.years_to_maturity(issue)
    band = state.band_of(issue)

    ytm_result = calculate_ytm(issue, as_of)
    ytm = ytm_result.ytm if ytm_result.ok else None

    assessment = PositionAssessment(
        isin=issue.isin,
        secid=issue.secid,
        security_name=issue.security_name,
        issuer_name=issue.issuer_name,
        industry=issue.industry,
        quantity=holding.quantity,
        value=holding.value,
        share=state.share(holding.value),
        rating=issue.rating,
        years_to_maturity=years,
        band_code=band.code if band else None,
        duration=macaulay_duration(issue, as_of, ytm),
        ytm=ytm,
    )

    # --- риск: единственный повод рассмотреть продажу ---------------------
    risk = check_risk_quality(issue, cfg, as_of)
    if not risk.passed:
        assessment.statuses.append(PositionStatus.CONSIDER_SELL)
        assessment.reasons.extend(c.message for c in risk.checks)
    for warning in risk.warnings:
        assessment.warnings.append(warning.message)
        # Недостаточно данных — «проверить риск», а не продажа: ухудшение не
        # подтверждено, и правил для продажи в этом случае нет.
        if PositionStatus.CHECK_RISK not in assessment.statuses:
            assessment.statuses.append(PositionStatus.CHECK_RISK)

    # Нет цены или НКД — стоимость позиции неизвестна, риск проверить нельзя.
    if issue.dirty_price() is None:
        if PositionStatus.CHECK_RISK not in assessment.statuses:
            assessment.statuses.append(PositionStatus.CHECK_RISK)
        assessment.warnings.append("Нет цены или НКД — стоимость позиции не определена")

    # --- можно ли докупать -------------------------------------------------
    # Проверяется минимальный лот, а не крупная покупка: вопрос шага 2 —
    # сохраняет ли бумага право быть купленной в принципе.
    if not issue.min_lot or issue.min_lot <= 0:
        assessment.statuses.append(PositionStatus.DO_NOT_ADD)
        assessment.reasons.append("Не определён размер лота")
    elif issue.dirty_price() is not None:
        for violation in violations(check_purchase(state, issue, issue.min_lot, cfg)):
            if PositionStatus.DO_NOT_ADD not in assessment.statuses:
                assessment.statuses.append(PositionStatus.DO_NOT_ADD)
            assessment.limit_notes.append(
                f"{violation.label}: {violation.before:.1%} → {violation.after:.1%} "
                f"при лимите {violation.limit:.0%}"
            )

    # Бумага перестала проходить требования раздела 3 — докупать нельзя, но это
    # не продажа: оферта или плавающий купон не ухудшают кредитный риск.
    # Причины риска сюда не повторяются — они уже записаны выше.
    eligibility = check_eligibility(issue, cfg, as_of)
    if not eligibility.passed:
        if PositionStatus.DO_NOT_ADD not in assessment.statuses:
            assessment.statuses.append(PositionStatus.DO_NOT_ADD)
        risk_codes = {c.code for c in risk.checks} | {w.code for w in risk.warnings}
        for check in eligibility.checks:
            if check.code not in risk_codes:
                assessment.reasons.append(f"Докупка невозможна: {check.message}")

    if not assessment.statuses:
        assessment.statuses.append(PositionStatus.HOLD)

    return assessment


def assess_all(
    state: PortfolioState,
    cfg: RebalanceConfig,
    as_of: date,
) -> list[PositionAssessment]:
    """Статусы всех позиций портфеля."""
    return [assess_position(h, state, cfg, as_of) for h in state.holdings]
