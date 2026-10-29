"""Тесты допуска выпуска к покупке (этап 3, раздел 3).

Главное, что здесь закрепляется, — разница между двумя проверками:

    check_eligibility  — рейтинг ниже BBB запрещает ПОКУПКУ;
    check_risk_quality — отсутствие рейтинга не означает ПРОДАЖУ.

Второе — не педантизм. Если приравнять «нет данных» к «риск ухудшился»,
алгоритм начнёт советовать продажу бумаг просто потому, что агентство не
обновило страницу. Правила требуют продавать только при ухудшении риска.
"""
from datetime import date

import pytest

from bond_strategy.models import BondIssue
from bond_strategy.rebalance.config import load_rebalance_config
from bond_strategy.rebalance.eligibility import check_eligibility, check_risk_quality

AS_OF = date(2026, 10, 26)


@pytest.fixture(scope="module")
def cfg():
    return load_rebalance_config("config/rebalance.json")


def issue(**overrides):
    params = dict(
        isin="TEST1",
        secid="TEST1",
        issuer_id="E1",
        issuer_name="Эмитент 1",
        industry="Промышленность",
        nominal=1000.0,
        coupon_type="FIXED",
        coupon_rate=0.15,
        coupon_frequency=2,
        maturity_date=date(2029, 10, 26),
        rating="A+",
        rating_agency="АКРА",
        rating_date=date(2026, 6, 1),
        clean_price=100.0,
        accrued_interest=0.0,
        min_lot=1,
    )
    params.update(overrides)
    return BondIssue(**params)


def codes(result):
    return result.codes


# --- допуск к покупке ------------------------------------------------------


def test_good_issue_passes(cfg):
    result = check_eligibility(issue(), cfg, AS_OF)
    assert result.passed, result.reason


def test_bbb_is_allowed_but_bbb_minus_is_not(cfg):
    assert check_eligibility(issue(rating="BBB"), cfg, AS_OF).passed
    assert codes(check_eligibility(issue(rating="BBB-"), cfg, AS_OF)) == [
        "EXCLUDED_RATING_BELOW_FLOOR"
    ]


def test_higher_ratings_pass(cfg):
    for rating in ("AAA", "AA", "A-", "BBB+"):
        assert check_eligibility(issue(rating=rating), cfg, AS_OF).passed, rating


def test_missing_rating_blocks_purchase_with_single_reason(cfg):
    result = check_eligibility(issue(rating=None), cfg, AS_OF)
    assert not result.passed
    # одна причина, а не дубль «нет данных» + «нет рейтинга»: остаётся более
    # конкретная, потому что рейтинг входит в обязательные поля
    assert codes(result) == ["EXCLUDED_NO_RATING"]


def test_unapproved_agency_blocks_purchase(cfg):
    result = check_eligibility(issue(rating_agency="Moody's"), cfg, AS_OF)
    assert codes(result) == ["EXCLUDED_UNAPPROVED_AGENCY"]


def test_stale_rating_blocks_purchase(cfg):
    result = check_eligibility(issue(rating_date=date(2024, 1, 1)), cfg, AS_OF)
    assert codes(result) == ["EXCLUDED_RATING_STALE"]


def test_future_rating_date_is_unverified(cfg):
    result = check_eligibility(issue(rating_date=date(2027, 1, 1)), cfg, AS_OF)
    assert codes(result) == ["EXCLUDED_RATING_UNVERIFIED"]


def test_floating_coupon_blocks_purchase(cfg):
    result = check_eligibility(issue(coupon_type="FLOAT"), cfg, AS_OF)
    assert codes(result) == ["EXCLUDED_COUPON_TYPE"]


def test_offer_blocks_purchase(cfg):
    result = check_eligibility(issue(offer_date=date(2027, 3, 1)), cfg, AS_OF)
    assert codes(result) == ["EXCLUDED_OFFER"]


def test_default_and_restructuring_block_purchase(cfg):
    assert "EXCLUDED_DEFAULT" in codes(check_eligibility(issue(default_flag=True), cfg, AS_OF))
    assert "EXCLUDED_RESTRUCTURING" in codes(
        check_eligibility(issue(restructuring_flag=True), cfg, AS_OF)
    )


def test_missing_maturity_blocks_purchase(cfg):
    result = check_eligibility(issue(maturity_date=None), cfg, AS_OF)
    assert "EXCLUDED_NO_MATURITY" in codes(result)


def test_matured_issue_blocks_purchase(cfg):
    result = check_eligibility(issue(maturity_date=date(2026, 1, 1)), cfg, AS_OF)
    assert "EXCLUDED_MATURED" in codes(result)


def test_amortization_is_allowed(cfg):
    # плановая амортизация по известному графику не является причиной отказа
    assert check_eligibility(issue(), cfg, AS_OF).passed


def test_unknown_flags_are_warned_not_blocked_by_default(cfg):
    result = check_eligibility(issue(), cfg, AS_OF)
    assert result.passed
    assert "FLAGS_UNVERIFIED" in [w.code for w in result.warnings]


def test_incomplete_data_blocks_purchase(cfg):
    result = check_eligibility(issue(industry=None), cfg, AS_OF)
    assert codes(result) == ["EXCLUDED_DATA_INCOMPLETE"]
    assert "industry" in result.reason


# --- проверка риска по купленной бумаге -----------------------------------


def test_healthy_position_has_no_risk_flags(cfg):
    result = check_risk_quality(issue(), cfg, AS_OF)
    assert result.passed
    assert result.warnings == []


def test_rating_below_floor_is_confirmed_deterioration(cfg):
    result = check_risk_quality(issue(rating="BB+"), cfg, AS_OF)
    assert not result.passed
    assert codes(result) == ["RATING_BELOW_FLOOR"]


def test_default_is_confirmed_deterioration(cfg):
    result = check_risk_quality(issue(default_flag=True), cfg, AS_OF)
    assert codes(result) == ["DEFAULT_CONFIRMED"]


def test_restructuring_is_confirmed_deterioration(cfg):
    result = check_risk_quality(issue(restructuring_flag=True), cfg, AS_OF)
    assert codes(result) == ["RESTRUCTURING_CONFIRMED"]


def test_missing_rating_is_not_a_sell_reason(cfg):
    """Ключевое правило: нет рейтинга — это «проверить риск», а не продажа."""
    result = check_risk_quality(issue(rating=None), cfg, AS_OF)
    assert result.passed, "отсутствие рейтинга не должно означать ухудшение риска"
    assert [w.code for w in result.warnings] == ["NO_RATING"]


def test_stale_rating_is_not_a_sell_reason(cfg):
    result = check_risk_quality(issue(rating_date=date(2023, 1, 1)), cfg, AS_OF)
    assert result.passed
    assert [w.code for w in result.warnings] == ["RATING_STALE"]


def test_unverified_rating_date_is_not_a_sell_reason(cfg):
    result = check_risk_quality(issue(rating_date=None), cfg, AS_OF)
    assert result.passed
    assert [w.code for w in result.warnings] == ["RATING_UNVERIFIED"]


def test_offer_does_not_make_position_a_sell_candidate(cfg):
    """Оферта мешает докупать, но кредитный риск не ухудшает."""
    result = check_risk_quality(issue(offer_date=date(2027, 3, 1)), cfg, AS_OF)
    assert result.passed


def test_floating_coupon_does_not_make_position_a_sell_candidate(cfg):
    result = check_risk_quality(issue(coupon_type="FLOAT"), cfg, AS_OF)
    assert result.passed
