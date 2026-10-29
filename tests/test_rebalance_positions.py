"""Тесты статусов действующих позиций (этап 5, шаг 2 раздела 5).

Ключевое различие, которое здесь закрепляется:

    подтверждённое ухудшение риска   -> «рассмотреть продажу»;
    недостаточно данных              -> «проверить риск», НЕ продажа;
    лимит или раздел 3               -> «не докупать», НЕ продажа.

Второй и третий случаи — самая вероятная ошибка реализации: соблазн объявить
любое отклонение причиной продажи велик, но правила разрешают продажу только
при ухудшении риска.
"""
from datetime import date

import pytest

from bond_strategy.models import BondIssue
from bond_strategy.rebalance.config import load_rebalance_config
from bond_strategy.rebalance.positions import (
    NOT_A_SELL_REASON,
    PositionAssessment,
    PositionStatus,
    assess_all,
    assess_position,
)
from bond_strategy.rebalance.snapshot import Holding, PortfolioState

AS_OF = date(2026, 10, 26)


@pytest.fixture(scope="module")
def cfg():
    return load_rebalance_config("config/rebalance.json")


def issue(
    isin="A",
    issuer="E1",
    industry="Промышленность",
    rating="A+",
    rating_date=date(2026, 6, 1),
    years=3,
    price=100.0,
    nkd=0.0,
    **extra,
):
    params = dict(
        isin=isin,
        secid=isin,
        issuer_id=issuer,
        issuer_name=f"Эмитент {issuer}",
        industry=industry,
        nominal=1000.0,
        coupon_type="FIXED",
        coupon_rate=0.15,
        coupon_frequency=2,
        maturity_date=date(AS_OF.year + years, 10, 26),
        rating=rating,
        rating_agency="АКРА",
        rating_date=rating_date,
        clean_price=price,
        accrued_interest=nkd,
        min_lot=1,
        security_name=f"Бумага {isin}",
    )
    params.update(extra)
    return BondIssue(**params)


def state(cfg, holdings=(), cash=100_000.0):
    return PortfolioState(
        as_of=AS_OF,
        cash=cash,
        holdings=list(holdings),
        issuer_groups=cfg.groups,
        band_of=cfg.band_for,
        horizon_years=cfg.horizon_years,
        low_rating_levels=cfg.low_rating_levels,
    )


def balanced_state(cfg):
    """Портфель из четырёх небольших бумаг — позиция здорова по всем лимитам."""
    holdings = [
        Holding(issue("A", "E1", "Промышленность"), 2),
        Holding(issue("B", "E2", "Нефть"), 2),
        Holding(issue("C", "E3", "Банки"), 2),
        Holding(issue("D", "E4", "Связь"), 2),
    ]
    return state(cfg, holdings, cash=92_000.0)


# --- здоровые позиции ------------------------------------------------------


def test_healthy_position_is_hold(cfg):
    s = balanced_state(cfg)
    for assessment in assess_all(s, cfg, AS_OF):
        assert assessment.status is PositionStatus.HOLD, assessment.reasons
        assert assessment.can_add


def test_hold_and_do_not_add_can_coexist(cfg):
    """Позиция без ухудшения риска, но с исчерпанным лимитом."""
    # одна бумага на 8% при лимите на выпуск 5%
    s = state(cfg, [Holding(issue("A", "E1"), 8)], cash=92_000.0)
    assessment = assess_position(s.holdings[0], s, cfg, AS_OF)
    assert PositionStatus.DO_NOT_ADD in assessment.statuses
    assert PositionStatus.CONSIDER_SELL not in assessment.statuses
    assert assessment.status is PositionStatus.DO_NOT_ADD
    assert not assessment.can_add


# --- подтверждённое ухудшение риска ---------------------------------------


def test_rating_below_floor_is_consider_sell(cfg):
    s = state(cfg, [Holding(issue("A", "E1", rating="BB"), 2)], cash=98_000.0)
    assessment = assess_position(s.holdings[0], s, cfg, AS_OF)
    assert assessment.status is PositionStatus.CONSIDER_SELL
    assert assessment.needs_sell
    assert any("BB" in r for r in assessment.reasons)


def test_default_is_consider_sell(cfg):
    s = state(cfg, [Holding(issue("A", "E1", default_flag=True), 2)], cash=98_000.0)
    assessment = assess_position(s.holdings[0], s, cfg, AS_OF)
    assert assessment.status is PositionStatus.CONSIDER_SELL
    assert any("дефолт" in r.lower() for r in assessment.reasons)


def test_restructuring_is_consider_sell(cfg):
    s = state(cfg, [Holding(issue("A", "E1", restructuring_flag=True), 2)], cash=98_000.0)
    assessment = assess_position(s.holdings[0], s, cfg, AS_OF)
    assert assessment.needs_sell


# --- недостаточно данных: проверить риск, но не продавать -----------------


def test_missing_rating_is_check_risk_not_sell(cfg):
    s = state(cfg, [Holding(issue("A", "E1", rating=None), 2)], cash=98_000.0)
    assessment = assess_position(s.holdings[0], s, cfg, AS_OF)
    assert PositionStatus.CHECK_RISK in assessment.statuses
    assert not assessment.needs_sell, "отсутствие рейтинга не повод продавать"
    assert assessment.warnings


def test_stale_rating_is_check_risk_not_sell(cfg):
    s = state(cfg, [Holding(issue("A", "E1", rating_date=date(2023, 1, 1)), 2)], cash=98_000.0)
    assessment = assess_position(s.holdings[0], s, cfg, AS_OF)
    assert PositionStatus.CHECK_RISK in assessment.statuses
    assert not assessment.needs_sell


def test_unpriced_position_is_check_risk(cfg):
    s = state(cfg, [Holding(issue("A", "E1", price=None, nkd=None), 2)], cash=98_000.0)
    assessment = assess_position(s.holdings[0], s, cfg, AS_OF)
    assert PositionStatus.CHECK_RISK in assessment.statuses
    assert not assessment.needs_sell
    assert assessment.value == 0.0
    assert any("Нет цены" in w for w in assessment.warnings)


# --- раздел 3: докупать нельзя, но продавать незачем ---------------------


def test_offer_blocks_adding_but_is_not_a_sell(cfg):
    s = state(cfg, [Holding(issue("A", "E1", offer_date=date(2027, 3, 1)), 2)], cash=98_000.0)
    assessment = assess_position(s.holdings[0], s, cfg, AS_OF)
    assert PositionStatus.DO_NOT_ADD in assessment.statuses
    assert not assessment.needs_sell, "оферта не ухудшает кредитный риск"
    assert not assessment.can_add


def test_floating_coupon_blocks_adding_but_is_not_a_sell(cfg):
    s = state(cfg, [Holding(issue("A", "E1", coupon_type="FLOAT"), 2)], cash=98_000.0)
    assessment = assess_position(s.holdings[0], s, cfg, AS_OF)
    assert PositionStatus.DO_NOT_ADD in assessment.statuses
    assert not assessment.needs_sell


def test_reasons_are_not_duplicated(cfg):
    """Одна и та же причина не должна попадать в отчёт дважды."""
    s = state(cfg, [Holding(issue("A", "E1", rating="BB"), 2)], cash=98_000.0)
    assessment = assess_position(s.holdings[0], s, cfg, AS_OF)
    assert len(assessment.reasons) == len(set(assessment.reasons))


# --- приоритет главного статуса ------------------------------------------


def test_sell_status_wins_over_do_not_add(cfg):
    s = state(cfg, [Holding(issue("A", "E1", rating="BB"), 8)], cash=92_000.0)
    assessment = assess_position(s.holdings[0], s, cfg, AS_OF)
    assert PositionStatus.DO_NOT_ADD in assessment.statuses
    assert assessment.status is PositionStatus.CONSIDER_SELL


def test_status_priority_order():
    def make(statuses):
        return PositionAssessment(
            isin="X",
            secid="X",
            security_name=None,
            issuer_name="E",
            industry="I",
            quantity=1,
            value=1000.0,
            share=0.01,
            rating="A+",
            years_to_maturity=2.0,
            band_code="1-2",
            duration=1.9,
            ytm=0.15,
            statuses=statuses,
        )

    assert make([PositionStatus.HOLD]).status is PositionStatus.HOLD
    assert make([PositionStatus.HOLD, PositionStatus.DO_NOT_ADD]).status is PositionStatus.DO_NOT_ADD
    assert (
        make([PositionStatus.CHECK_RISK, PositionStatus.DO_NOT_ADD]).status
        is PositionStatus.CHECK_RISK
    )
    assert (
        make(
            [PositionStatus.CONSIDER_SELL, PositionStatus.CHECK_RISK, PositionStatus.DO_NOT_ADD]
        ).status
        is PositionStatus.CONSIDER_SELL
    )


def test_not_a_sell_reason_documents_what_is_not_a_sell_trigger():
    """Текст для отчёта перечисляет то, что причиной продажи не является."""
    lowered = NOT_A_SELL_REASON.lower()
    for word in ("цен", "ставок", "лимит", "срок"):
        assert word in lowered, word


# --- метаданные позиции ---------------------------------------------------


def test_assessment_carries_duration_and_ytm(cfg):
    s = balanced_state(cfg)
    assessment = assess_all(s, cfg, AS_OF)[0]
    assert assessment.duration is not None and assessment.duration > 0
    assert assessment.ytm is not None and assessment.ytm > 0
    assert assessment.band_code is not None


def test_assessment_serialises(cfg):
    s = balanced_state(cfg)
    data = assess_all(s, cfg, AS_OF)[0].as_dict()
    assert data["status"] == "HOLD"
    assert data["status_label"] == "Держать"
    assert data["isin"] == "A"
    assert isinstance(data["statuses"], list)


def test_empty_portfolio_has_no_assessments(cfg):
    assert assess_all(state(cfg, [], 0.0), cfg, AS_OF) == []
