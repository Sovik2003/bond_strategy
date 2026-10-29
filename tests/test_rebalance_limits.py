"""Тесты лимитов концентрации (этап 4, раздел 2.1).

Арифметика подобрана явно, потому что доля считается от растущего знаменателя:
покупка переводит деньги в бумаги, стоимость портфеля не меняется, но доля
считается от стоимости «после». Поэтому «купить N бумаг = N%» верно только когда
в портфеле нет денег, и в тестах это учтено.

Лимит на выпуск (5%) срабатывает раньше лимита на отрасль (25%) в портфеле из
одной бумаги. Чтобы изолировать лимит отрасли, нужно несколько выпусков в
отрасли — иначе тест проверял бы не тот лимит, который заявлен в названии.

Главное, что здесь закрепляется, — правило «не ухудшать»: при уже превышенном
лимите покупка разрешена, если она это превышение не увеличивает.
"""
from datetime import date

import pytest

from bond_strategy.models import BondIssue
from bond_strategy.rebalance.config import load_rebalance_config
from bond_strategy.rebalance.limits import (
    EPS,
    LimitCheck,
    check_purchase,
    existing_breaches,
    violations,
)
from bond_strategy.rebalance.snapshot import Holding, PortfolioState

AS_OF = date(2026, 10, 26)


@pytest.fixture(scope="module")
def cfg():
    return load_rebalance_config("config/rebalance.json")


def issue(isin="A", issuer="E1", industry="Промышленность", rating="A+", years=3):
    return BondIssue(
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
        rating_date=date(2026, 6, 1),
        clean_price=100.0,
        accrued_interest=0.0,
        min_lot=1,
    )


def state(cfg, holdings=(), cash=100_000.0, groups=None):
    return PortfolioState(
        as_of=AS_OF,
        cash=cash,
        holdings=list(holdings),
        issuer_groups=groups if groups is not None else cfg.groups,
        band_of=cfg.band_for,
        horizon_years=cfg.horizon_years,
        low_rating_levels=cfg.low_rating_levels,
    )


def codes(checks):
    return [c.code for c in violations(checks)]


def find(checks, code):
    return next(c for c in checks if c.code == code)


# --- лимит на выпуск ------------------------------------------------------


def test_issue_limit_allows_exactly_five_percent(cfg):
    """5 бумаг из 100 000 ₽ — ровно 5%; 6 бумаг дают 6 000/101 000 = 5.94%."""
    s = state(cfg)
    assert codes(check_purchase(s, issue(), 5, cfg)) == []
    assert codes(check_purchase(s, issue(), 6, cfg)) == ["ISSUE_LIMIT"]


def test_float_noise_does_not_trigger_limit(cfg):
    s = state(cfg)
    check = find(check_purchase(s, issue(), 5, cfg), "ISSUE_LIMIT")
    assert not check.blocks
    assert check.after <= 0.05 + EPS


# --- лимит на эмитента ----------------------------------------------------


def test_issuer_limit_counts_all_issues_of_one_issuer(cfg):
    """Три выпуска одного эмитента по 3% дают 9%; четвёртый переводит за 10%."""
    holdings = [
        Holding(issue("A1", "E1"), 3),
        Holding(issue("A2", "E1"), 3),
        Holding(issue("A3", "E1"), 3),
    ]
    s = state(cfg, holdings, cash=91_000.0)
    assert s.exposure().issuer["E1"] == pytest.approx(0.09)

    # новый выпуск того же эмитента на 1% проходит, на 2% — нет
    assert codes(check_purchase(s, issue("A4", "E1"), 1, cfg)) == []
    blocking = codes(check_purchase(s, issue("A4", "E1"), 2, cfg))
    assert "ISSUER_LIMIT" in blocking
    # лимит на выпуск при этом не нарушен: 2 000/102 000 = 1.96%
    assert "ISSUE_LIMIT" not in blocking


# --- лимит на группу связанных компаний -----------------------------------


def test_group_limit_merges_two_issuers(cfg):
    """E1 и E2 в одной группе: 6% + 3% = 9%, добавка 2% выводит за 10%."""
    groups = {"E1": "G", "E2": "G"}
    holdings = [
        Holding(issue("A1", "E1"), 3),
        Holding(issue("A2", "E1"), 3),
        Holding(issue("B1", "E2"), 3),
    ]
    s = state(cfg, holdings, cash=91_000.0, groups=groups)
    exposure = s.exposure()
    assert exposure.group["G"] == pytest.approx(0.09)
    # по отдельности лимит эмитента не нарушен ни у кого
    assert exposure.issuer["E1"] == pytest.approx(0.06)

    blocking = codes(check_purchase(s, issue("B2", "E2"), 2, cfg))
    assert "GROUP_LIMIT" in blocking


def test_group_equals_issuer_when_no_map(cfg):
    """Без карты групп эмитент сам себе группа — двойного нарушения не возникает."""
    s = state(cfg, [Holding(issue("A1", "E1"), 4)], cash=96_000.0)
    exposure = s.exposure()
    assert exposure.group["E1"] == pytest.approx(exposure.issuer["E1"])


# --- лимит на отрасль -----------------------------------------------------


def test_industry_limit_blocks_new_issue_when_industry_is_full(cfg):
    """Пять выпусков по 5% заполняют отрасль до 25%; шестой её переполнит."""
    holdings = [Holding(issue(f"P{i}", f"PE{i}", "Промышленность"), 5) for i in range(5)]
    s = state(cfg, holdings, cash=75_000.0)
    assert s.exposure().industry["Промышленность"] == pytest.approx(0.25)

    blocking = codes(check_purchase(s, issue("P9", "PE9", "Промышленность"), 1, cfg))
    assert "INDUSTRY_LIMIT" in blocking
    # сам новый выпуск был бы всего 990/101 000 = 0.98% — лимит выпуска не при чём
    assert "ISSUE_LIMIT" not in blocking

    # бумага другой отрасли проходит
    assert codes(check_purchase(s, issue("N1", "NE1", "Нефть"), 1, cfg)) == []


# --- правило «не ухудшать» -----------------------------------------------


def test_breached_industry_does_not_block_other_industry(cfg):
    """Прямое требование: превышение в одной отрасли не блокирует другие."""
    s = state(cfg, [Holding(issue("A", "E1", "Промышленность"), 30)], cash=70_000.0)
    assert s.exposure().industry["Промышленность"] == pytest.approx(0.30)

    assert codes(check_purchase(s, issue("B", "E2", "Нефть"), 5, cfg)) == []


def test_breached_industry_blocks_worsening_it(cfg):
    s = state(cfg, [Holding(issue("A", "E1", "Промышленность"), 30)], cash=70_000.0)
    assert "INDUSTRY_LIMIT" in codes(
        check_purchase(s, issue("B", "E2", "Промышленность"), 5, cfg)
    )


def test_over_limit_category_allows_purchase_outside_it(cfg):
    """Категория BBB выше 25%, но покупка бумаги A+ её долю снижает — можно."""
    holdings = [Holding(issue(f"L{i}", f"LE{i}", rating="BBB"), 10) for i in range(3)]
    s = state(cfg, holdings, cash=70_000.0)
    assert s.exposure().low_rating == pytest.approx(0.30)

    check = find(check_purchase(s, issue("H", "HE", rating="A+"), 5, cfg), "LOW_RATING_LIMIT")
    assert check.preexisting, "превышение было и до сделки"
    # покупка переводит деньги в бумаги, стоимость портфеля не меняется,
    # поэтому доля BBB остаётся прежней — она не растёт, и этого достаточно
    assert check.after == pytest.approx(check.before)
    assert not check.blocks

    # а покупка ещё одной BBB увеличивает долю категории — блокируется
    blocking = find(check_purchase(s, issue("L9", "LE9", rating="BBB"), 5, cfg), "LOW_RATING_LIMIT")
    assert blocking.blocks


def test_over_horizon_allows_shorter_paper_when_above_limit(cfg):
    """Срок свыше 5 лет уже 10%+; короткая бумага снижает долю — разрешена."""
    holdings = [Holding(issue(f"Y{i}", f"YE{i}", years=7), 2) for i in range(5)]
    s = state(cfg, holdings, cash=90_000.0)
    assert s.exposure().over_horizon == pytest.approx(0.10)

    check = find(check_purchase(s, issue("S1", "SE1", years=3), 5, cfg), "OVER_HORIZON_LIMIT")
    assert check.after <= check.before
    assert not check.blocks


def test_limit_check_properties():
    raised = LimitCheck(code="X", label="X", limit=0.05, before=0.04, after=0.06)
    assert raised.exceeds and raised.worsened and raised.blocks
    assert not raised.preexisting

    unchanged = LimitCheck(code="X", label="X", limit=0.05, before=0.06, after=0.06)
    assert unchanged.exceeds and unchanged.preexisting
    assert not unchanged.worsened
    assert not unchanged.blocks, "неизменное превышение не блокирует покупку"

    reduced = LimitCheck(code="X", label="X", limit=0.05, before=0.30, after=0.28)
    assert reduced.exceeds and reduced.preexisting
    assert not reduced.blocks, "снижение превышения — улучшение, а не нарушение"


# --- лимит срока более пяти лет ------------------------------------------


def test_over_horizon_limit_allows_ten_percent(cfg):
    """Пять длинных выпусков по 2% дают ровно 10%; шестой выводит за предел."""
    holdings = [Holding(issue(f"Y{i}", f"YE{i}", years=7), 2) for i in range(5)]
    s = state(cfg, holdings, cash=90_000.0)
    assert s.exposure().over_horizon == pytest.approx(0.10)

    blocking = codes(check_purchase(s, issue("Y9", "YE9", years=7), 1, cfg))
    assert "OVER_HORIZON_LIMIT" in blocking
    assert "ISSUE_LIMIT" not in blocking


def test_short_issue_does_not_count_toward_horizon(cfg):
    s = state(cfg, [Holding(issue("Y", "YE", years=7), 5)], cash=95_000.0)
    check = find(check_purchase(s, issue("Z", "ZE", years=3), 5, cfg), "OVER_HORIZON_LIMIT")
    assert check.before == pytest.approx(check.after)
    assert not check.blocks


# --- лимит категории BBB/BBB+ --------------------------------------------


def test_bbb_plus_counts_toward_category(cfg):
    s = state(cfg, [Holding(issue("A", "E1", rating="BBB+"), 10)], cash=90_000.0)
    assert s.exposure().low_rating == pytest.approx(0.10)


def test_bbb_minus_is_not_in_category(cfg):
    """BBB− не допускается к покупке и в ограниченную категорию не входит."""
    s = state(cfg, [Holding(issue("A", "E1", rating="BBB-"), 10)], cash=90_000.0)
    assert s.exposure().low_rating == pytest.approx(0.0)


def test_a_rating_does_not_count_toward_category(cfg):
    s = state(cfg, [Holding(issue("A", "E1", rating="A-"), 10)], cash=90_000.0)
    assert s.exposure().low_rating == pytest.approx(0.0)


# --- превышения, существующие до сделки ----------------------------------


def test_existing_breaches_reports_preexisting(cfg):
    s = state(cfg, [Holding(issue("A", "E1", "Промышленность"), 30)], cash=70_000.0)
    breaches = existing_breaches(s, cfg)
    assert any(key.startswith("INDUSTRY_LIMIT") for key in breaches)
    assert any(key.startswith("ISSUE_LIMIT") for key in breaches)
    assert any(key.startswith("ISSUER_LIMIT") for key in breaches)


def test_no_breaches_in_balanced_portfolio(cfg):
    holdings = [
        Holding(issue("A", "E1", "Промышленность"), 4),
        Holding(issue("B", "E2", "Нефть"), 4),
        Holding(issue("C", "E3", "Банки"), 4),
        Holding(issue("D", "E4", "Связь"), 4),
    ]
    s = state(cfg, holdings, cash=84_000.0)
    assert existing_breaches(s, cfg) == {}


def test_zero_quantity_returns_no_checks(cfg):
    assert check_purchase(state(cfg), issue(), 0, cfg) == []


def test_all_checks_are_reported_even_when_passing(cfg):
    """Отчёт показывает таблицу «до/после» по всем лимитам, а не только нарушения."""
    checks = check_purchase(state(cfg), issue(), 1, cfg)
    assert {c.code for c in checks} == {
        "ISSUE_LIMIT",
        "ISSUER_LIMIT",
        "GROUP_LIMIT",
        "INDUSTRY_LIMIT",
        "LOW_RATING_LIMIT",
        "OVER_HORIZON_LIMIT",
    }
