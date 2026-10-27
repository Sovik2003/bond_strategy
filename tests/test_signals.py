import pytest

from bond_strategy.config import load_config
from bond_strategy.models import SignalType
from bond_strategy.portfolio.engine import Portfolio
from bond_strategy.signals.generator import evaluate_issue, make_signal_id
from tests.facts import (
    TEST_DATE,
    defaulted,
    illiquid,
    medium_quality,
    no_price,
    no_rating,
    normal,
    stale_price,
    with_offer,
)


@pytest.fixture()
def cfg():
    return load_config("config/strategy.json")


@pytest.fixture()
def portfolio(cfg):
    return Portfolio(cfg=cfg, cash=cfg.initial_capital)


def px(*issues):
    return {i.isin: (i.clean_price, i.accrued_interest) for i in issues}


def test_normal_gives_buy(portfolio, cfg):
    issue = normal()
    signal = evaluate_issue(issue, portfolio, px(issue), TEST_DATE, cfg)
    assert signal.signal_type is SignalType.BUY, signal.failed_conditions
    assert signal.score >= cfg.min_buy_score
    assert signal.suggested_amount >= cfg.min_position
    assert signal.suggested_quantity > 0


def test_buy_amount_within_issuer_limit(portfolio, cfg):
    issue = normal()
    signal = evaluate_issue(issue, portfolio, px(issue), TEST_DATE, cfg)
    assert signal.suggested_amount <= cfg.initial_capital * 0.25 + 1e-6


def test_reserve_respected(portfolio, cfg):
    issue = normal()
    signal = evaluate_issue(issue, portfolio, px(issue), TEST_DATE, cfg)
    assert portfolio.cash - signal.suggested_amount >= cfg.reserve_base - 1e-6


def test_offer_is_blocked(portfolio, cfg):
    issue = with_offer()
    signal = evaluate_issue(issue, portfolio, px(issue), TEST_DATE, cfg)
    assert signal.signal_type is SignalType.DATA_BLOCKED


def test_default_never_buys(portfolio, cfg):
    issue = defaulted()
    assert evaluate_issue(issue, portfolio, px(issue), TEST_DATE, cfg).signal_type is not SignalType.BUY


def test_stale_never_buys(portfolio, cfg):
    issue = stale_price()
    assert evaluate_issue(issue, portfolio, px(issue), TEST_DATE, cfg).signal_type is SignalType.DATA_BLOCKED


def test_missing_price_never_buys(portfolio, cfg):
    issue = no_price()
    assert evaluate_issue(issue, portfolio, px(issue), TEST_DATE, cfg).signal_type is SignalType.DATA_BLOCKED


def test_illiquid_never_buys(portfolio, cfg):
    issue = illiquid()
    assert evaluate_issue(issue, portfolio, px(issue), TEST_DATE, cfg).signal_type is not SignalType.BUY


def test_no_rating_never_buys(portfolio, cfg):
    issue = no_rating()
    assert evaluate_issue(issue, portfolio, px(issue), TEST_DATE, cfg).signal_type is SignalType.DATA_BLOCKED


def test_small_capital_cannot_buy(cfg):
    small = Portfolio(cfg=cfg, cash=11_000)
    issue = normal()
    signal = evaluate_issue(issue, small, px(issue), TEST_DATE, cfg)
    assert signal.signal_type is not SignalType.BUY


def test_low_premium_goes_to_watch(portfolio, cfg):
    issue = normal()
    issue.ofz_ytm = 0.20  # ОФЗ выше доходности выпуска -> премия отрицательная
    signal = evaluate_issue(issue, portfolio, px(issue), TEST_DATE, cfg)
    assert signal.signal_type is not SignalType.BUY
    assert any("LOW_PREMIUM" in f or "LOW_SCORE" in f for f in signal.failed_conditions)


def test_medium_quality_evaluable(cfg):
    p = Portfolio(cfg=cfg, cash=cfg.initial_capital)
    issue = medium_quality()
    signal = evaluate_issue(issue, p, px(issue), TEST_DATE, cfg)
    assert signal.signal_type in {SignalType.BUY, SignalType.WATCH}


def test_signal_id_stable(portfolio, cfg):
    issue = normal()
    a = evaluate_issue(issue, portfolio, px(issue), TEST_DATE, cfg)
    b = evaluate_issue(issue, portfolio, px(issue), TEST_DATE, cfg)
    assert a.signal_id == b.signal_id


def test_signal_id_differs_by_type():
    a = make_signal_id("X", SignalType.BUY, "r", TEST_DATE)
    b = make_signal_id("X", SignalType.WATCH, "r", TEST_DATE)
    assert a != b


def test_rerun_is_deterministic(cfg):
    runs = []
    for _ in range(3):
        p = Portfolio(cfg=cfg, cash=cfg.initial_capital)
        issue = normal()
        runs.append(evaluate_issue(issue, p, px(issue), TEST_DATE, cfg))
    assert len({r.signal_id for r in runs}) == 1
    assert len({r.score for r in runs}) == 1
    assert len({r.suggested_amount for r in runs}) == 1


def test_signal_carries_config_id(portfolio, cfg):
    signal = evaluate_issue(normal(), portfolio, px(normal()), TEST_DATE, cfg)
    assert signal.config_id == cfg.config_id
