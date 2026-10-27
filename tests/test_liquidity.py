"""Ликвидность из истории торгов.

Проверки построены на реальной особенности ответа /history: строка приходит
на каждый торговый день, включая дни без сделок, и дублируется по бордам.
"""
from datetime import date

import pytest

from bond_strategy.ingestion.liquidity import (
    average_daily_turnover,
    value_by_day,
)


def row(day: str, board: str, value):
    return {"TRADEDATE": day, "BOARDID": board, "VALUE": value}


# ---------- сведение дней ----------


def test_zero_value_day_counts_as_no_trading_not_missing():
    """VALUE=0 — это состоявшийся день без сделок, а не пропуск данных."""
    days = value_by_day([row("2026-10-26", "TQCB", 0)])
    assert days == {date(2026, 10, 26): 0.0}


def test_none_value_is_missing_not_zero():
    """None — данных нет. Такой день в расчёт не идёт."""
    assert value_by_day([row("2026-10-26", "TQOD", None)]) == {}


def test_same_day_on_two_boards_not_double_counted():
    """TQCB с оборотом и TQOD с None — это один день, а не два."""
    rows = [row("2026-10-26", "TQCB", 500.0), row("2026-10-26", "TQOD", None)]
    assert value_by_day(rows) == {date(2026, 10, 26): 500.0}


def test_max_board_wins_when_both_have_values():
    """Если данные есть на двух бордах, берётся максимум, а не сумма."""
    rows = [row("2026-10-26", "TQCB", 500.0), row("2026-10-26", "TQOD", 300.0)]
    assert value_by_day(rows) == {date(2026, 10, 26): 500.0}


def test_unparseable_date_skipped():
    assert value_by_day([{"TRADEDATE": None, "BOARDID": "TQCB", "VALUE": 1.0}]) == {}


# ---------- средний оборот ----------


def test_average_divides_by_trading_days_not_by_trading_with_volume():
    """Делитель — все торговые дни окна, включая нулевые.

    Иначе бумага, торговавшаяся дважды за месяц, выглядела бы ликвиднее
    той, что торгуется каждый день теми же объёмами.
    """
    rows = [
        row("2026-10-22", "TQCB", 1000.0),
        row("2026-10-23", "TQCB", 0),
        row("2026-10-24", "TQCB", 0),
        row("2026-10-25", "TQCB", 0),
    ]
    avg, days = average_daily_turnover(rows, 20)
    assert days == 4
    assert avg == pytest.approx(250.0)  # 1000/4, а не 1000/1


def test_window_takes_last_n_days():
    rows = [row(f"2026-10-{d:02d}", "TQCB", 100.0) for d in range(1, 11)]
    rows.append(row("2026-10-11", "TQCB", 900.0))
    avg, days = average_daily_turnover(rows, 3)
    assert days == 3
    # последние три дня: 09, 10, 11 -> (100+100+900)/3
    assert avg == pytest.approx(1100.0 / 3)


def test_empty_history_is_none_not_zero():
    """Нет истории — нет значения. Ноль означал бы «не торгуется»."""
    assert average_daily_turnover([], 20) == (None, 0)


def test_all_none_values_is_none():
    assert average_daily_turnover([row("2026-10-26", "TQOD", None)], 20) == (None, 0)


def test_real_shape_two_boards_many_days():
    """Форма реального ответа: 18 дней, каждый по два борда."""
    rows = []
    for d in range(1, 19):
        day = f"2026-10-{d:02d}"
        rows.append(row(day, "TQCB", 1000.0))
        rows.append(row(day, "TQOD", None))
    avg, days = average_daily_turnover(rows, 20)
    assert days == 18
    assert avg == pytest.approx(1000.0)
