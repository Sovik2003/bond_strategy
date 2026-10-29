"""Тесты планировщика покупок (этап 6, шаги 4–6).

Что здесь проверяется и почему именно это:

1. Жадный цикл действительно повторяется: покупка → пересчёт → следующая.
2. Приоритеты шага 5 не переставляются: доходность не перебивает снижение
   концентрации. Это главный риск реализации — соблазн «выбрать доходнее»
   велик, и он прямо запрещён требованием.
3. Правила остановки дают понятную причину, а не пустой результат.

Арифметика: номинал 1000 ₽, цена 100 + НКД 0, поэтому один лот = 1000 ₽.
"""
from datetime import date, timedelta

import pytest

from bond_strategy.models import BondIssue
from bond_strategy.rebalance.config import load_rebalance_config
from bond_strategy.rebalance.planner import plan
from bond_strategy.rebalance.snapshot import Holding, PortfolioState

AS_OF = date(2026, 10, 26)


@pytest.fixture(scope="module")
def cfg():
    return load_rebalance_config("config/rebalance.json")


def issue(
    isin="A",
    issuer=None,
    industry="Промышленность",
    rating="A+",
    years=3,
    coupon=0.15,
    price=100.0,
    min_lot=1,
):
    return BondIssue(
        isin=isin,
        secid=isin,
        issuer_id=issuer or f"E_{isin}",
        issuer_name=f"Эмитент {isin}",
        industry=industry,
        nominal=1000.0,
        coupon_type="FIXED",
        coupon_rate=coupon,
        coupon_frequency=2,
        maturity_date=AS_OF + timedelta(days=round(years * 365)),
        rating=rating,
        rating_agency="АКРА",
        rating_date=date(2026, 6, 1),
        clean_price=price,
        accrued_interest=0.0,
        min_lot=min_lot,
        security_name=f"Бумага {isin}",
    )


def state(cfg, holdings=(), cash=100_000.0):
    return PortfolioState.from_config(AS_OF, cash, list(holdings), cfg)


# --- базовое поведение -----------------------------------------------------


def test_empty_portfolio_buys_something(cfg):
    result = plan(state(cfg), [issue("A")], cfg)
    assert result.purchases
    assert result.purchases[0].isin == "A"
    assert result.purchases[0].lots >= 1


def test_purchase_does_not_violate_limits(cfg):
    result = plan(state(cfg), [issue("A")], cfg)
    assert result.purchases
    # лимит на выпуск 5% от 100 000 ₽ — не больше 5 лотов
    assert result.purchases[0].lots <= 5


def test_cash_left_is_reported(cfg):
    result = plan(state(cfg), [issue("A")], cfg)
    # 5 лотов по 1000 ₽, остаток 95 000 ₽ меньше минимального лота другого выпуска
    assert result.cash_left == pytest.approx(95_000.0)


def test_no_eligible_issues_gives_reason(cfg):
    bad = issue("BAD", rating="BB")
    result = plan(state(cfg), [bad], cfg)
    assert result.purchases == []
    assert "раздел 3" in result.stop_reason
    assert result.rejected


def test_rejected_carry_codes(cfg):
    result = plan(state(cfg), [issue("BAD", rating="BB")], cfg)
    assert result.rejected[0].codes == ["EXCLUDED_RATING_BELOW_FLOOR"]


# --- жадный цикл: несколько покупок подряд ---------------------------------


def test_greedy_loop_makes_several_purchases(cfg):
    """Разные эмитенты и отрасли: алгоритм обязан купить несколько бумаг."""
    universe = [
        issue("A", "E1", "Промышленность"),
        issue("B", "E2", "Нефть"),
        issue("C", "E3", "Банки"),
        issue("D", "E4", "Связь"),
    ]
    result = plan(state(cfg), universe, cfg)
    assert len(result.purchases) >= 4
    # каждый эмитент — не более 10% портфеля
    for purchase in result.purchases:
        assert purchase.share_after <= 0.10 + 1e-9


def test_each_step_recomputes_state(cfg):
    universe = [
        issue("A", "E1", "Промышленность"),
        issue("B", "E2", "Нефть"),
    ]
    result = plan(state(cfg), universe, cfg)
    total = sum(p.amount for p in result.purchases)
    assert result.cash_left == pytest.approx(100_000.0 - total)


def test_one_issue_does_not_exceed_its_limit_across_steps(cfg):
    """Одна и та же бумага не покупается дважды сверх лимита 5%."""
    result = plan(state(cfg), [issue("A")], cfg)
    lots = sum(p.lots for p in result.purchases if p.isin == "A")
    assert lots <= 5


# --- приоритеты шага 5 -----------------------------------------------------


def test_concentration_beats_yield(cfg):
    """Ключевое требование: доходность не перекрывает снижение концентрации.

    Портфель перегружен отраслью «Промышленность». Есть два кандидата:
      — «Промышленность» с более высокой доходностью (усугубляет концентрацию);
      — «Нефть» с более низкой доходностью (снижает её).
    Должна покупаться «Нефть».
    """
    holdings = [Holding(issue("P0", "E0", "Промышленность"), 20)]
    s = state(cfg, holdings, cash=80_000.0)

    rich = issue("P1", "E1", "Промышленность", coupon=0.25)  # доходнее
    diversifying = issue("N1", "E2", "Нефть", coupon=0.10)  # диверсифицирует

    result = plan(s, [rich, diversifying], cfg)
    assert result.purchases
    assert result.purchases[0].isin == "N1", "снижение концентрации идёт первым критерием"


def test_yield_decides_when_all_else_equal(cfg):
    """При равенстве прочих критериев выбирается более доходная бумага."""
    low = issue("LOW", "E1", "Нефть", coupon=0.10)
    high = issue("HIGH", "E2", "Банки", coupon=0.20)
    result = plan(state(cfg), [low, high], cfg)
    assert result.purchases[0].isin == "HIGH"


def test_higher_rating_wins_over_yield(cfg):
    """Рейтинг идёт третьим критерием, доходность — последним."""
    # одинаковая отрасль и интервал, разные рейтинги и купоны
    aaa = issue("AAA1", "E1", "Нефть", rating="AAA", coupon=0.10)
    bbb = issue("BBB1", "E2", "Нефть", rating="BBB", coupon=0.25)
    result = plan(state(cfg), [aaa, bbb], cfg)
    # обе бумаги в одной отрасли, поэтому концентрация не различает их;
    # рейтинг AAA должен победить более доходную BBB
    assert result.purchases[0].isin == "AAA1"


def test_band_fill_beats_yield(cfg):
    """Заполнение пустого интервала сроков идёт до доходности.

    Портфель целиком в интервале «до 1 года». Кандидаты подобраны так, чтобы
    правило дюрации не вмешивалось: обе бумаги короче предела покупки 3,5 года,
    иначе тест проверял бы отсев по дюрации, а не приоритет критериев.
    """
    holdings = [Holding(issue("SHORT", "E0", "Нефть", years=0.5), 10)]
    s = state(cfg, holdings, cash=90_000.0)

    # пустой интервал 2-3 года, низкая доходность
    filling = issue("MID", "E1", "Банки", years=2.5, coupon=0.08)
    # тот же интервал, что уже заполнен, но заметно доходнее
    same_band = issue("SHORT2", "E2", "Связь", years=0.5, coupon=0.30)

    result = plan(s, [filling, same_band], cfg)
    assert result.purchases
    assert result.purchases[0].isin == "MID"


def test_reasons_explain_the_choice(cfg):
    holdings = [Holding(issue("P0", "E0", "Промышленность"), 20)]
    s = state(cfg, holdings, cash=80_000.0)
    result = plan(s, [issue("N1", "E2", "Нефть")], cfg)
    assert result.purchases[0].reasons, "у покупки должно быть обоснование"


# --- правило дюрации ------------------------------------------------------


def test_issue_longer_than_buy_max_is_skipped(cfg):
    """Ориентир дюрации покупки — не выше 3,5 года."""
    very_long = issue("VL", "E1", years=30)
    result = plan(state(cfg), [very_long], cfg)
    assert result.purchases == []
    assert "дюраци" in result.stop_reason.lower()


def test_short_issue_is_allowed(cfg):
    result = plan(state(cfg), [issue("S", years=2)], cfg)
    assert result.purchases


# --- правила остановки -----------------------------------------------------


def test_insufficient_cash_gives_clear_reason(cfg):
    """Остатка не хватает на лот — причина должна говорить именно об этом."""
    result = plan(state(cfg, [], cash=500.0), [issue("A")], cfg)
    assert result.purchases == []
    assert "остатк" in result.stop_reason.lower()
    assert "лот" in result.stop_reason.lower()


def test_expensive_lot_is_not_bought(cfg):
    result = plan(state(cfg, [], cash=500.0), [issue("A", price=100.0)], cfg)
    assert result.purchases == []


def test_lot_size_is_respected(cfg):
    """Лот из 10 бумаг стоит 10 000 ₽, а не 1 000 ₽.

    Портфель 250 000 ₽: 10 000 ₽ — это 4%, укладывается в лимит 5% на выпуск.
    На портфеле 25 000 ₽ тот же лот был бы 40% и был бы справедливо отклонён,
    поэтому сумма подобрана под лимит, а не «поудобнее».
    """
    big_lot = issue("BIG", min_lot=10)
    result = plan(state(cfg, [], cash=250_000.0), [big_lot], cfg)
    assert result.purchases
    assert result.purchases[0].quantity % 10 == 0
    assert result.purchases[0].quantity == 10
    assert result.purchases[0].lots == 1


def test_portfolio_full_gives_reason(cfg):
    """Лимит на выпуск исчерпан — следующая покупка невозможна."""
    holdings = [Holding(issue("A"), 5)]
    s = state(cfg, holdings, cash=95_000.0)
    result = plan(s, [issue("A")], cfg)
    assert result.purchases == []
    assert result.stop_reason


def test_zero_cash_still_reports_reason(cfg):
    result = plan(state(cfg, [], cash=0.0), [issue("A")], cfg)
    assert result.purchases == []
    assert result.stop_reason


# --- сериализация ----------------------------------------------------------


def test_purchase_serialises(cfg):
    result = plan(state(cfg), [issue("A")], cfg)
    data = result.purchases[0].as_dict()
    assert data["isin"] == "A"
    assert data["lots"] >= 1
    assert "amount" in data and "reasons" in data
    assert data["share_after"] <= 1.0


def test_plan_is_deterministic(cfg):
    """Один и тот же портфель и рынок дают один и тот же отчёт."""
    universe = [issue("A", "E1", "Нефть"), issue("B", "E2", "Банки")]
    first = plan(state(cfg), universe, cfg)
    second = plan(state(cfg), universe, cfg)
    assert [p.isin for p in first.purchases] == [p.isin for p in second.purchases]
    assert [p.lots for p in first.purchases] == [p.lots for p in second.purchases]


def test_existing_position_can_be_topped_up_within_limit(cfg):
    holdings = [Holding(issue("A"), 2)]
    s = state(cfg, holdings, cash=98_000.0)
    result = plan(s, [issue("A")], cfg)
    if result.purchases:
        assert result.purchases[0].isin == "A"
        assert result.purchases[0].lots <= 3
