from datetime import date

import pytest

from bond_strategy.analytics.cashflows import (
    build_schedule,
    outstanding_before,
    synthetic_coupon_dates,
    total_payout,
    xirr,
)
from bond_strategy.analytics.yield_calc import calculate_ytm, purchase_cost_per_bond
from bond_strategy.config import load_config
from tests.facts import TEST_DATE, amortizing, normal


@pytest.fixture(scope="module")
def cfg():
    return load_config("config/strategy.json")


def test_coupon_dates_end_at_maturity():
    dates = synthetic_coupon_dates(normal(), TEST_DATE)
    assert dates[-1] == normal().maturity_date
    assert all(d > TEST_DATE for d in dates)


def test_schedule_returns_nominal_once():
    schedule = build_schedule(normal(), TEST_DATE)
    redemption = [f for f in schedule if f.kind == "redemption"]
    assert len(redemption) == 1
    assert redemption[0].amount_per_bond == pytest.approx(1000.0)


def test_total_payout_exceeds_cost():
    issue = normal()
    cost = purchase_cost_per_bond(issue)
    payout = total_payout(build_schedule(issue, TEST_DATE))
    assert payout > cost  # купон 16% при цене ~98.5 должен окупаться


def test_amortization_reduces_nominal():
    issue = amortizing()
    assert outstanding_before(issue, date(2027, 1, 1)) == pytest.approx(1000.0)
    assert outstanding_before(issue, date(2027, 12, 2)) == pytest.approx(500.0)


def test_amortization_and_redemption_sum_to_nominal():
    issue = amortizing()
    schedule = build_schedule(issue, TEST_DATE)
    principal = sum(f.amount_per_bond for f in schedule if f.kind in {"amortization", "redemption"})
    assert principal == pytest.approx(1000.0)


def test_coupon_after_amortization_is_smaller():
    issue = amortizing()
    schedule = build_schedule(issue, TEST_DATE)
    early = [f for f in schedule if f.kind == "coupon" and f.payment_date <= date(2027, 6, 1)]
    late = [f for f in schedule if f.kind == "coupon" and f.payment_date > date(2027, 12, 1)]
    assert early and late
    assert late[0].amount_per_bond < early[0].amount_per_bond


def test_actual_coupon_schedule_wins_over_rate():
    issue = normal()
    issue.coupon_schedule = [(date(2027, 1, 15), 42.0)]
    schedule = build_schedule(issue, TEST_DATE)
    coupons = [f for f in schedule if f.kind == "coupon"]
    assert len(coupons) == 1
    assert coupons[0].amount_per_bond == pytest.approx(42.0)


def test_past_payments_are_excluded():
    issue = normal()
    issue.coupon_schedule = [(date(2026, 1, 1), 80.0), (date(2027, 1, 1), 80.0)]
    schedule = build_schedule(issue, TEST_DATE)
    assert all(f.payment_date > TEST_DATE for f in schedule)


def test_ytm_reasonable():
    r = calculate_ytm(normal(), TEST_DATE)
    assert r.ok, r.message
    assert 0.10 < r.ytm < 0.25


def test_ytm_fails_loudly_without_price():
    issue = normal()
    issue.clean_price = None
    r = calculate_ytm(issue, TEST_DATE)
    assert not r.ok and r.ytm is None


def test_ytm_fails_without_coupon():
    issue = normal()
    issue.coupon_rate = None
    issue.coupon_schedule = []
    r = calculate_ytm(issue, TEST_DATE)
    assert not r.ok


def test_ytm_accounts_commission():
    no_fee = calculate_ytm(normal(), TEST_DATE, 0.0)
    with_fee = calculate_ytm(normal(), TEST_DATE, 0.01)
    assert with_fee.ytm < no_fee.ytm


def test_xirr_simple():
    flows = [(date(2026, 1, 1), -1000.0), (date(2027, 1, 1), 1100.0)]
    assert xirr(flows) == pytest.approx(0.10, abs=0.005)


def test_xirr_none_without_sign_change():
    assert xirr([(date(2026, 1, 1), 1000.0), (date(2027, 1, 1), 1100.0)]) is None


def test_coupon_uses_current_nominal_not_initial():
    """Купон считается от ТЕКУЩЕГО номинала.

    У амортизированной бумаги `nominal` уже уменьшен, а `amortization` содержит
    только предстоящие платежи. Если базой брать `initial_nominal`, купоны
    выходят в разы больше, и доходность к погашению становится бессмысленной:
    на реальном выпуске с номиналом 50 ₽ из 1000 ₽ получалось 850% вместо 8,5%.
    """
    issue = normal()
    issue.nominal = 50.0
    issue.initial_nominal = 1000.0
    issue.coupon_rate = 0.0785
    issue.coupon_frequency = 2
    issue.coupon_schedule = []
    issue.amortization = []
    schedule = build_schedule(issue, TEST_DATE)
    coupons = [f for f in schedule if f.kind == "coupon"]
    assert coupons
    # 7,85% годовых от 50 ₽ при полугодовом купоне — около 1,96 ₽
    for flow in coupons:
        assert flow.amount_per_bond == pytest.approx(1.96, abs=0.01)


def test_amortized_paper_yield_is_sane():
    """Регрессия: доходность амортизированной бумаги не выходит за разумные пределы."""
    from bond_strategy.analytics.yield_calc import calculate_ytm

    issue = normal()
    issue.nominal = 50.0
    issue.initial_nominal = 1000.0
    issue.coupon_rate = 0.0785
    issue.coupon_schedule = []
    issue.amortization = []
    issue.clean_price = 94.1
    issue.accrued_interest = 0.1
    result = calculate_ytm(issue, TEST_DATE)
    assert result.ok, result.message
    assert result.ytm is not None
    assert -0.5 < result.ytm < 1.0, f"доходность {result.ytm:.2%} вне разумных пределов"
