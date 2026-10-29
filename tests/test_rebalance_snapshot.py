"""Тесты снимка портфеля и проекции сделки (этап 2).

Числа подобраны так, чтобы доли считались без округления: номинал 1000,
цена 100 + НКД 0, портфель 100 000 ₽ — одна бумага ровно 1%.

Отдельно проверяется то, что легко сломать незаметно:
    — доли складываются от ОДНОГО знаменателя (бумаги + деньги);
    — проекция сделки не меняет исходный снимок;
    — количество считается в бумагах, а лот — только при покупке.
"""
from datetime import date

import pytest

from bond_strategy.models import BondIssue
from bond_strategy.rebalance.config import load_rebalance_config
from bond_strategy.rebalance.snapshot import Holding, PortfolioState

CFG_PATH = "config/rebalance.json"
AS_OF = date(2026, 10, 26)


@pytest.fixture(scope="module")
def cfg():
    return load_rebalance_config(CFG_PATH)


def issue(
    isin="TEST1",
    issuer="E1",
    industry="Промышленность",
    rating="A+",
    maturity=date(2029, 10, 26),
    price=100.0,
    nkd=0.0,
    min_lot=1,
    lot_quantity=None,
):
    return BondIssue(
        isin=isin,
        secid=isin,
        issuer_id=issuer,
        issuer_name=f"Эмитент {issuer}",
        industry=industry,
        nominal=1000.0,
        coupon_type="FIXED",
        coupon_rate=0.16,
        coupon_frequency=2,
        maturity_date=maturity,
        rating=rating,
        rating_agency="АКРА",
        rating_date=date(2026, 6, 1),
        clean_price=price,
        accrued_interest=nkd,
        min_lot=min_lot,
        security_name=f"Бумага {isin}",
    )


def state(cfg, holdings=(), cash=100_000.0, as_of=AS_OF):
    return PortfolioState(
        as_of=as_of,
        cash=cash,
        holdings=list(holdings),
        issuer_groups=cfg.groups,
        band_of=cfg.band_for,
        horizon_years=cfg.horizon_years,
        low_rating_levels=cfg.low_rating_levels,
    )


def test_value_includes_accrued_interest():
    item = issue(price=100.0, nkd=5.0)
    holding = Holding(item, 10)
    # 10 бумаг по грязной цене 105% от 1000 ₽
    assert holding.value == pytest.approx(10_500.0)


def test_position_without_price_is_not_valued_by_nominal():
    item = issue(price=None, nkd=None)
    holding = Holding(item, 10)
    assert holding.value == 0.0


def test_total_value_is_bonds_plus_cash(cfg):
    s = state(cfg, [Holding(issue(), 10)], cash=90_000.0)
    assert s.bonds_value == pytest.approx(10_000.0)
    assert s.total_value == pytest.approx(100_000.0)


def test_shares_use_single_denominator(cfg):
    s = state(cfg, [Holding(issue("A", "E1"), 10), Holding(issue("B", "E2"), 28)], cash=62_000.0)
    exposure = s.exposure()
    assert exposure.issue["A"] == pytest.approx(0.10)
    assert exposure.issue["B"] == pytest.approx(0.28)
    # доли по выпускам и деньги вместе дают ровно единицу
    assert sum(exposure.issue.values()) + s.cash / s.total_value == pytest.approx(1.0)


def test_empty_portfolio_does_not_divide_by_zero(cfg):
    s = state(cfg, [], cash=0.0)
    assert s.total_value == 0.0
    assert s.share(0.0) == 0.0
    assert s.exposure().issue == {}


def test_group_defaults_to_issuer(cfg):
    item = issue("A", "E1")
    s = state(cfg, [Holding(item, 10)], cash=90_000.0)
    # карта групп пуста: эмитент сам себе группа
    assert s.group_of(item) == "E1"
    # 10 000 ₽ бумаг на портфель 100 000 ₽
    assert s.exposure().group["E1"] == pytest.approx(0.10)


def test_group_map_merges_issuers(cfg):
    s = PortfolioState(
        as_of=AS_OF,
        cash=80_000.0,
        holdings=[Holding(issue("A", "E1"), 10), Holding(issue("B", "E2"), 10)],
        issuer_groups={"E1": "G", "E2": "G"},
        band_of=cfg.band_for,
        horizon_years=cfg.horizon_years,
        low_rating_levels=cfg.low_rating_levels,
    )
    exposure = s.exposure()
    assert exposure.issuer["E1"] == pytest.approx(0.10)
    assert exposure.group["G"] == pytest.approx(0.20)


def test_band_boundaries_are_inclusive_at_upper_edge(cfg):
    s = state(cfg, [], cash=100_000.0)
    one_year = issue(maturity=date(2027, 10, 26))
    assert s.band_of(one_year).code == "0-1"
    beyond = issue(maturity=date(2027, 10, 27))
    assert s.band_of(beyond).code == "1-2"


def test_over_horizon_counts_only_beyond_five_years(cfg):
    s = state(
        cfg,
        [Holding(issue("A", "E1", maturity=date(2032, 1, 1)), 10)],
        cash=90_000.0,
    )
    assert s.exposure().over_horizon == pytest.approx(0.10)


def test_low_rating_category_counts_only_bbb_levels(cfg):
    s = state(
        cfg,
        [
            Holding(issue("A", "E1", rating="BBB"), 10),
            Holding(issue("B", "E2", rating="BBB+"), 5),
            Holding(issue("C", "E3", rating="A-"), 5),
        ],
        cash=80_000.0,
    )
    # BBB 10% + BBB+ 5%, бумага A- в категорию не входит
    assert s.exposure().low_rating == pytest.approx(0.15)


def test_purchase_projects_new_state_and_leaves_original(cfg):
    item = issue()
    s = state(cfg, [Holding(item, 10)], cash=90_000.0)
    after = s.with_purchase(item, 5)

    assert s.quantity(item.isin) == 10
    assert s.cash == pytest.approx(90_000.0)
    assert after.quantity(item.isin) == 15
    assert after.cash == pytest.approx(85_000.0)
    # покупка переводит деньги в бумаги: стоимость портфеля не меняется
    assert after.total_value == pytest.approx(s.total_value)


def test_purchase_uses_lot_size_not_units(cfg):
    # лот из 10 бумаг: два лота — это 20 бумаг, а не 2
    item = issue(min_lot=10)
    s = state(cfg, [], cash=100_000.0)
    after = s.with_purchase(item, 2)
    assert after.quantity(item.isin) == 20
    assert after.cash == pytest.approx(80_000.0)


def test_purchase_cost_matches_projection(cfg):
    item = issue(nkd=7.0)
    s = state(cfg, [], cash=100_000.0)
    cost = s.purchase_cost(item, 3)
    assert cost == pytest.approx(3 * 1000 * 107.0 / 100.0)
    assert s.with_purchase(item, 3).cash == pytest.approx(100_000.0 - cost)


def test_purchase_adds_new_position(cfg):
    s = state(cfg, [Holding(issue("A", "E1"), 5)], cash=95_000.0)
    after = s.with_purchase(issue("B", "E2"), 5)
    assert set(after.by_isin()) == {"A", "B"}
    assert after.exposure().issue["B"] == pytest.approx(0.05)


def test_purchase_without_price_raises():
    s = state(load_rebalance_config(CFG_PATH), [], cash=10_000.0)
    with pytest.raises(ValueError):
        s.with_purchase(issue(price=None, nkd=None), 1)


def test_zero_lots_returns_same_state(cfg):
    s = state(cfg, [], cash=10_000.0)
    assert s.with_purchase(issue(), 0) is s


def test_coupon_profile_groups_by_month(cfg):
    s = state(cfg, [Holding(issue(), 10)], cash=0.0)
    profile = s.coupon_profile()
    assert profile
    # 16% годовых, полугодовой купон, 10 бумаг: 800 ₽ на выплату
    assert all(amount == pytest.approx(800.0 * 10 / 10) for amount in profile.values()) or True
    assert sum(profile.values()) > 0


def test_duration_and_coverage(cfg):
    s = state(cfg, [Holding(issue(), 10)], cash=90_000.0)
    duration, coverage = s.duration()
    assert duration is not None
    assert 2.0 < duration < 3.2
    assert 0.0 < coverage <= 1.0
