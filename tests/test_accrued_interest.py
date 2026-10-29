"""НКД — рубли на бумагу, цена — проценты от номинала.

Отдельный тест-модуль, потому что эта разница уже приводила к тихой ошибке:
`ACCRUEDINT` в MOEX ISS приходит в рублях, а `PREVPRICE` — в процентах, и
сложение «в лоб» завышало стоимость бумаги на величину НКД. На реальных данных
это давало отрицательную доходность у бумаг с купоном 14–16% годовых:
у выпуска с купоном 69,81 ₽ и НКД 54,85 ₽ выходило «153,96%» вместо 104,6%,
то есть цена в полтора раза выше рыночной.

Проверка на живых данных (2026-10-26, выпуск RU000A0JXTY7): MOEX публикует
доходность 14,98%, расчёт с НКД в рублях даёт 15,80%, с НКД в процентах —
минус 39,98%. Разница между 14,98% и 15,80% объясняется конвенцией: MOEX
считает эффективную годовую доходность, здесь — XIRR.
"""
from datetime import date

import pytest

from bond_strategy.models import BondIssue
from bond_strategy.analytics.cashflows import build_schedule
from bond_strategy.analytics.yield_calc import calculate_ytm, purchase_cost_per_bond
from bond_strategy.portfolio.engine import Portfolio, position_size


def issue(**overrides):
    params = dict(
        isin="RU000TEST0001",
        secid="TEST1",
        issuer_id="E1",
        issuer_name="Эмитент",
        industry="Промышленность",
        nominal=1000.0,
        initial_nominal=1000.0,
        coupon_type="FIXED",
        coupon_rate=0.16,
        coupon_frequency=2,
        maturity_date=date(2029, 6, 1),
        rating="A+",
        rating_agency="АКРА",
        rating_date=date(2026, 6, 1),
        clean_price=98.5,
        accrued_interest=7.5,
        min_lot=1,
    )
    params.update(overrides)
    return BondIssue(**params)


def test_accrued_interest_is_rubles_not_percent():
    """7,5 ₽ НКД — это 0,75% от номинала 1000 ₽, а не 7,5%."""
    item = issue(accrued_interest=7.5)
    assert item.accrued_interest_pct() == pytest.approx(0.75)


def test_dirty_price_is_in_percent_because_clean_price_is():
    """Грязная цена возвращается в тех же единицах, что чистая."""
    item = issue(clean_price=98.5, accrued_interest=7.5)
    # 98,5% + 0,75% = 99,25%, а не 106,0%
    assert item.dirty_price() == pytest.approx(99.25)


def test_purchase_cost_adds_rubles_to_rubles():
    item = issue(clean_price=100.0, accrued_interest=5.0)
    # 100% от 1000 ₽ плюс 5 ₽ НКД, а не 105% от 1000 ₽
    assert purchase_cost_per_bond(item) == pytest.approx(1005.0)


def test_large_accrued_interest_does_not_inflate_cost():
    """Ключевой случай: НКД 54,85 ₽ при цене 99,11% — это 104,6%, а не 154%."""
    item = issue(clean_price=99.11, accrued_interest=54.85)
    assert item.dirty_price() == pytest.approx(104.595, abs=1e-9)
    assert purchase_cost_per_bond(item) == pytest.approx(1045.95, abs=1e-6)


def test_portfolio_buy_uses_rubles_for_accrued_interest():
    from bond_strategy.config import load_config

    cfg = load_config("config/strategy.json")
    portfolio = Portfolio(cfg=cfg, cash=50_000.0)
    item = issue(clean_price=98.5, accrued_interest=7.5)
    trade = portfolio.buy(item, 10, date(2026, 10, 26))
    # 10 * (985 ₽ + 7,5 ₽) = 9 925 ₽
    assert trade.amount == pytest.approx(9_925.0)
    assert portfolio.cash == pytest.approx(50_000.0 - 9_925.0)


def test_position_size_uses_rubles_for_accrued_interest():
    from bond_strategy.config import load_config

    cfg = load_config("config/strategy.json")
    portfolio = Portfolio(cfg=cfg, cash=50_000.0)
    item = issue(clean_price=100.0, accrued_interest=50.0, min_lot=1)
    prices = {item.isin: (item.clean_price, item.accrued_interest)}
    # одна бумага стоит 1000 + 50 = 1050 ₽; при 50 000 ₽ и лимите 25% на выпуск
    # это 12 бумаг (12 600 ₽), а не 13 (13 650 ₽)
    qty = position_size(portfolio, item, prices, cfg)
    assert qty * 1050.0 <= 0.25 * 50_000.0
    if qty:
        assert (qty + 1) * 1050.0 > 0.25 * 50_000.0


def test_yield_is_positive_for_high_coupon_paper():
    """Регрессия: купон 16% при цене у номинала не может давать минус."""
    item = issue(clean_price=100.0, accrued_interest=6.0, coupon_rate=0.16)
    result = calculate_ytm(item, date(2026, 10, 26))
    assert result.ok, result.message
    assert result.ytm is not None and result.ytm > 0.10


def test_yield_matches_schedule_payouts():
    """Доходность считается от полного графика, а не от одной выплаты."""
    item = issue(clean_price=100.0, accrued_interest=0.0, coupon_rate=0.16)
    schedule = build_schedule(item, date(2026, 10, 26))
    coupons = [f for f in schedule if f.kind == "coupon"]
    # 16% годовых, полугодовой купон, расчёт 2026-10-26, погашение 2029-06-01:
    # 2026-12, 2027-06, 2027-12, 2028-06, 2028-12, 2029-06 — шесть выплат
    assert len(coupons) == 6
    assert all(f.amount_per_bond == pytest.approx(80.0) for f in coupons)
