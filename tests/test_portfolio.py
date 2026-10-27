from datetime import date

import pytest

from bond_strategy.config import load_config
from bond_strategy.models import OperationType, RiskMode
from bond_strategy.portfolio.engine import (
    LimitViolation,
    Portfolio,
    check_limits,
    position_size,
)
from tests.facts import TEST_DATE, amortizing, medium_quality, normal


@pytest.fixture()
def cfg():
    return load_config("config/strategy.json")


@pytest.fixture()
def portfolio(cfg):
    return Portfolio(cfg=cfg, cash=cfg.initial_capital)


def prices(*issues):
    return {i.isin: (i.clean_price, i.accrued_interest) for i in issues}


def test_buy_deducts_full_amount(portfolio):
    issue = normal()
    trade = portfolio.buy(issue, 10, TEST_DATE)
    expected = 10 * (98.5 + 7.5) / 100 * 1000
    assert trade.amount == pytest.approx(expected)
    assert portfolio.cash == pytest.approx(50000 - expected)
    assert portfolio.positions[issue.isin].quantity == 10


def test_commission_charged(portfolio):
    issue = normal()
    trade = portfolio.buy(issue, 10, TEST_DATE, commission_rate=0.01)
    assert trade.commission == pytest.approx(106.0)
    assert trade.amount == pytest.approx(10_706.0)


def test_duplicate_buy_rejected(portfolio):
    issue = normal()
    portfolio.buy(issue, 10, TEST_DATE)
    with pytest.raises(LimitViolation) as exc:
        portfolio.buy(issue, 10, TEST_DATE)
    assert exc.value.code == "DUPLICATE"


def test_insufficient_cash_rejected(portfolio):
    with pytest.raises(LimitViolation) as exc:
        portfolio.buy(normal(), 1000, TEST_DATE)
    assert exc.value.code == "NO_CASH"


def test_reserve_limit(portfolio, cfg):
    p = Portfolio(cfg=cfg, cash=12_000)
    with pytest.raises(LimitViolation) as exc:
        check_limits(p, normal(), 2_500, prices(normal()), cfg)
    assert exc.value.code == "RESERVE_LIMIT"


def test_issuer_limit(portfolio, cfg):
    with pytest.raises(LimitViolation) as exc:
        check_limits(portfolio, normal(), 13_000, prices(normal()), cfg)
    assert exc.value.code == "ISSUER_LIMIT"


def test_industry_limit(cfg):
    a, b = normal(), medium_quality()
    b.industry = a.industry
    b.issuer_id = "ISS_OTHER"   # другой эмитент, чтобы упереться именно в отрасль
    p = Portfolio(cfg=cfg, cash=50_000)
    p.buy(a, 12, TEST_DATE)  # 12 720 ₽ = 25.4% портфеля в отрасли
    px = {a.isin: (a.clean_price, a.accrued_interest)}
    # 9 000 ₽ проходит лимит эмитента (18%), но выводит отрасль на 43% > 40%
    with pytest.raises(LimitViolation) as exc:
        check_limits(p, b, 9_000, px, cfg)
    assert exc.value.code == "INDUSTRY_LIMIT"


def test_position_size_respects_reserve(portfolio, cfg):
    issue = normal()
    qty = position_size(portfolio, issue, prices(issue), cfg)
    assert qty > 0
    amount = qty * (98.5 + 7.5) / 100 * 1000
    assert portfolio.cash - amount >= cfg.reserve_base - 1e-6


def test_position_size_zero_when_no_room(cfg):
    p = Portfolio(cfg=cfg, cash=9_000)
    assert position_size(p, normal(), prices(normal()), cfg) == 0


def test_coupon_once(portfolio):
    issue = normal()
    portfolio.buy(issue, 10, TEST_DATE)
    first = portfolio.process_coupon(issue.isin, date(2026, 12, 1), 80.0)
    second = portfolio.process_coupon(issue.isin, date(2026, 12, 1), 80.0)
    assert first == pytest.approx(800.0)
    assert second == 0.0
    assert portfolio.positions[issue.isin].received_coupons == pytest.approx(800.0)


def test_amortization_once_and_nominal_reduced(portfolio):
    issue = amortizing()
    portfolio.buy(issue, 10, TEST_DATE)
    cash_before = portfolio.cash
    paid = portfolio.process_amortization(issue.isin, date(2027, 6, 1), 250.0)
    assert paid == pytest.approx(2500.0)
    assert portfolio.cash == pytest.approx(cash_before + 2500.0)
    assert portfolio.positions[issue.isin].nominal_outstanding == pytest.approx(750.0)
    assert portfolio.process_amortization(issue.isin, date(2027, 6, 1), 250.0) == 0.0
    assert portfolio.positions[issue.isin].nominal_outstanding == pytest.approx(750.0)


def test_market_value_uses_outstanding_nominal(portfolio):
    issue = amortizing()
    portfolio.buy(issue, 10, TEST_DATE)
    portfolio.process_amortization(issue.isin, date(2027, 6, 1), 250.0)
    pos = portfolio.positions[issue.isin]
    assert pos.market_value(99.0, 5.0) == pytest.approx(10 * 104.0 / 100 * 750.0)


def test_redemption_closes_position(portfolio):
    issue = normal()
    portfolio.buy(issue, 10, TEST_DATE)
    assert portfolio.process_redemption(issue.isin, issue.maturity_date) == pytest.approx(10_000.0)
    assert issue.isin not in portfolio.positions


def test_sell_reduces_quantity_and_cost(portfolio):
    issue = normal()
    portfolio.buy(issue, 10, TEST_DATE)
    cost_before = portfolio.positions[issue.isin].purchase_cost
    portfolio.sell(issue, 4, TEST_DATE)
    pos = portfolio.positions[issue.isin]
    assert pos.quantity == 6
    assert pos.purchase_cost == pytest.approx(cost_before * 0.6)


def test_sell_all_removes_position(portfolio):
    issue = normal()
    portfolio.buy(issue, 10, TEST_DATE)
    portfolio.sell(issue, 10, TEST_DATE)
    assert issue.isin not in portfolio.positions


def test_drawdown_and_risk_mode(portfolio):
    issue = normal()
    portfolio.buy(issue, 12, TEST_DATE)
    px = prices(issue)
    portfolio.update_peak(px)
    assert portfolio.risk_mode(px) is RiskMode.NORMAL

    crashed = {issue.isin: (issue.clean_price * 0.4, issue.accrued_interest)}
    assert portfolio.drawdown(crashed) < -0.08
    assert portfolio.risk_mode(crashed) in {
        RiskMode.ENHANCED_CONTROL,
        RiskMode.CAPITAL_PROTECTION,
    }


def test_medium_quality_share_capped(cfg):
    p = Portfolio(cfg=cfg, cash=50_000)
    issue = medium_quality()
    qty = position_size(p, issue, prices(issue), cfg)
    amount = qty * (96.0 + 7.5) / 100 * 1000
    assert amount <= 50_000 * 0.25 + 1e-6


def test_trade_is_immutable(portfolio):
    trade = portfolio.buy(normal(), 10, TEST_DATE)
    assert trade.operation_type is OperationType.BUY
    with pytest.raises(Exception):
        trade.amount = 0.0
