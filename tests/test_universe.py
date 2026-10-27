"""Юниверсум: дешёвый отсев по борде и полнота учёта отказов.

Проверки идут на словарях в форме строки борды TQCB — без сети.
"""
from datetime import date

import pytest

from bond_strategy.config import load_config
from bond_strategy.universe import UniverseStats, screen, screen_board_row

AS_OF = date(2026, 10, 26)


@pytest.fixture(scope="module")
def cfg():
    return load_config("config/strategy.json")


def board_row(**over) -> dict:
    """Строка борды, проходящая все дешёвые проверки. Поля переопределяются."""
    row = {
        "SECID": "RU000TEST0001",
        "FACEUNIT": "SUR",
        "MATDATE": "2028-06-01",
        "LISTLEVEL": 1,
        "PREVPRICE": 98.5,
        "ACCRUEDINT": 7.5,
        "BONDTYPE": "Облигация с фиксированным (известным) купоном",
        "COUPON_DETAILS": "Фиксированный с известными купонами",
        "OFFERDATE": None,
    }
    row.update(over)
    return row


def check(row, cfg):
    stats = UniverseStats(board_rows=1)
    passed = screen_board_row(row, AS_OF, cfg, stats)
    return passed, stats


# ---------- пропуск ----------


def test_clean_row_passes(cfg):
    passed, stats = check(board_row(), cfg)
    assert passed is True
    assert stats.as_dict()["rejected_currency"] == 0


def test_rub_currency_passes(cfg):
    assert check(board_row(FACEUNIT="RUB"), cfg)[0] is True


# ---------- валюта ----------


@pytest.mark.parametrize("unit", ["USD", "CNY", "EUR", "CHF"])
def test_foreign_currency_rejected(cfg, unit):
    passed, stats = check(board_row(FACEUNIT=unit), cfg)
    assert passed is False
    assert stats.rejected_currency == 1


def test_missing_faceunit_treated_as_rouble(cfg):
    """Отсутствие валюты — это рублёвый выпуск, а не повод отбросить."""
    row = board_row()
    del row["FACEUNIT"]
    assert check(row, cfg)[0] is True


# ---------- срок ----------


def test_short_maturity_rejected(cfg):
    passed, stats = check(board_row(MATDATE="2026-12-01"), cfg)
    assert passed is False
    assert stats.rejected_maturity == 1


def test_maturity_exactly_at_limit_passes(cfg):
    """Ровно на границе min_days_to_maturity выпуск проходит, а не отсекается."""
    from datetime import timedelta

    min_days = int(cfg.get("filters", "min_days_to_maturity"))
    edge = (AS_OF + timedelta(days=min_days)).isoformat()
    assert check(board_row(MATDATE=edge), cfg)[0] is True


def test_one_day_short_is_rejected(cfg):
    from datetime import timedelta

    min_days = int(cfg.get("filters", "min_days_to_maturity"))
    edge = (AS_OF + timedelta(days=min_days - 1)).isoformat()
    assert check(board_row(MATDATE=edge), cfg)[0] is False


def test_no_maturity_rejected(cfg):
    passed, stats = check(board_row(MATDATE=None), cfg)
    assert passed is False
    assert stats.rejected_no_maturity == 1


def test_broken_maturity_rejected(cfg):
    passed, stats = check(board_row(MATDATE="не дата"), cfg)
    assert passed is False
    assert stats.rejected_no_maturity == 1


# ---------- листинг ----------


def test_high_list_level_rejected(cfg):
    passed, stats = check(board_row(LISTLEVEL=3), cfg)
    assert passed is False
    assert stats.rejected_list_level == 1


def test_missing_list_level_passes(cfg):
    assert check(board_row(LISTLEVEL=None), cfg)[0] is True


# ---------- цена ----------


def test_no_price_rejected(cfg):
    passed, stats = check(board_row(PREVPRICE=None), cfg)
    assert passed is False
    assert stats.rejected_no_price == 1


def test_zero_accrued_interest_is_not_missing(cfg):
    """НКД = 0 это валидное значение (день выплаты купона), а не пропуск."""
    assert check(board_row(ACCRUEDINT=0), cfg)[0] is True


def test_none_accrued_interest_rejected(cfg):
    assert check(board_row(ACCRUEDINT=None), cfg)[0] is False


# ---------- тип купона ----------


def test_floating_rejected_by_board_type(cfg):
    passed, stats = check(
        board_row(BONDTYPE="Облигация с плавающим купоном", COUPON_DETAILS=None), cfg
    )
    assert passed is False
    assert stats.rejected_coupon_type == 1


def test_structured_rejected(cfg):
    passed, stats = check(board_row(BONDTYPE="Структурная облигация"), cfg)
    assert passed is False
    assert stats.rejected_coupon_type == 1


def test_amortizing_with_fixed_coupon_passes(cfg):
    """Амортизируемая облигация — это про погашение, а не про купон."""
    assert check(board_row(BONDTYPE="Амортизируемая облигация"), cfg)[0] is True


# ---------- оферта ----------


def test_future_offer_rejected(cfg):
    passed, stats = check(board_row(OFFERDATE="2027-03-01"), cfg)
    assert passed is False
    assert stats.rejected_offer == 1


def test_past_offer_passes(cfg):
    assert check(board_row(OFFERDATE="2025-03-01"), cfg)[0] is True


def test_offer_today_passes(cfg):
    """Оферта ровно сегодня уже не будущая — выпуск не отсекается."""
    assert check(board_row(OFFERDATE=AS_OF.isoformat()), cfg)[0] is True


# ---------- полнота учёта ----------


def test_every_row_is_accounted_for(cfg):
    """Ни одна строка не исчезает молча: прошедшие + отказы = все строки."""
    rows = [
        board_row(SECID="A"),
        board_row(SECID="B", FACEUNIT="USD"),
        board_row(SECID="C", MATDATE="2026-11-01"),
        board_row(SECID="D", LISTLEVEL=3),
        board_row(SECID="E", PREVPRICE=None),
        board_row(SECID="F", BONDTYPE="Облигация с плавающим купоном"),
        board_row(SECID="G", OFFERDATE="2027-01-01"),
    ]
    passed, stats = screen(rows, AS_OF, cfg)
    d = stats.as_dict()
    total = (
        d["passed_screen"]
        + d["rejected_currency"]
        + d["rejected_maturity"]
        + d["rejected_list_level"]
        + d["rejected_no_price"]
        + d["rejected_coupon_type"]
        + d["rejected_offer"]
    )
    assert total == len(rows) == stats.board_rows
    assert [r["SECID"] for r in passed] == ["A"]


def test_screen_is_pure_and_repeatable(cfg):
    rows = [board_row(SECID="A"), board_row(SECID="B", FACEUNIT="USD")]
    first = screen(rows, AS_OF, cfg)
    second = screen(rows, AS_OF, cfg)
    assert first[1].as_dict() == second[1].as_dict()
    assert [r["SECID"] for r in first[0]] == [r["SECID"] for r in second[0]]


def test_stats_do_not_leak_between_calls(cfg):
    """Счётчики не накапливаются: каждый вызов считает заново."""
    screen([board_row(FACEUNIT="USD")], AS_OF, cfg)
    _, stats = screen([board_row()], AS_OF, cfg)
    assert stats.rejected_currency == 0
