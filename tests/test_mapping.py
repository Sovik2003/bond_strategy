"""Разбор реальных ответов MOEX ISS. Фикстуры сняты с настоящего выпуска.

Выпуск RU000A10FEP7 (СФО СБ Секьюритизация 5 кл.АС) выбран намеренно:
у него 106 амортизационных выплат и текущий номинал ниже исходного.
"""
import json
from datetime import date
from pathlib import Path

import pytest

from bond_strategy.config import load_config
from bond_strategy.ingestion.mapping import (
    amortization_payments,
    build_issue,
    future_coupons,
    load_issuers,
    quality_for,
)
from bond_strategy.ingestion.moex import (
    IssClient,
    IssError,
    block,
    coupon_type_from,
    dt,
    num,
)
from bond_strategy.models import DataStatus, Quality

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def as_records(name: str) -> list[dict]:
    payload = load(name)
    return [dict(zip(payload["columns"], row)) for row in payload["data"]]


@pytest.fixture(scope="module")
def description() -> dict:
    return load("iss_description.json")


@pytest.fixture(scope="module")
def bondization() -> dict:
    return {
        "coupons": as_records("iss_coupons.json"),
        "amortizations": as_records("iss_amortizations_full.json"),
        "offers": [],
    }


@pytest.fixture(scope="module")
def cfg():
    return load_config("config/strategy.json")


# ---------- примитивы ----------


def test_block_raises_on_missing_block():
    with pytest.raises(IssError):
        block({"securities": {"columns": [], "data": []}}, "coupons")


def test_block_raises_on_ragged_row():
    with pytest.raises(IssError):
        block({"x": {"columns": ["A", "B"], "data": [[1]]}}, "x")


def test_num_distinguishes_zero_from_missing():
    assert num(0) == 0.0
    assert num(None) is None
    assert num("") is None
    assert num("abc") is None


def test_dt_parses_and_rejects():
    assert dt("2026-10-26") == date(2026, 10, 26)
    assert dt("2026-10-26 12:00:00") == date(2026, 10, 26)
    assert dt("") is None
    assert dt(None) is None


# ---------- тип купона ----------


def test_amortizing_bond_type_is_not_a_coupon_type():
    """BOND_TYPE='Амортизируемая облигация' описывает погашение, а не купон."""
    assert coupon_type_from("Амортизируемая облигация", None, None) == "FIXED"


def test_floating_detected_by_benchmark():
    assert coupon_type_from("Облигация с плавающим купоном", "RREFKEYR", None) == "FLOATING"


def test_floating_detected_by_bond_type_without_benchmark():
    assert coupon_type_from("Облигация с плавающим купоном", None, None) == "FLOATING"


def test_foreign_and_structured():
    assert coupon_type_from("Валютная облигация", None, None) == "FOREIGN"
    assert coupon_type_from("Облигация структурная", None, None) == "STRUCTURED"


def test_unknown_type_is_not_assumed_fixed():
    assert coupon_type_from("Нечто непонятное", None, None) == "UNKNOWN"


# ---------- амортизация ----------


def test_maturity_row_excluded_from_amortization(bondization):
    """Строка data_source='maturity' — это погашение, не амортизация."""
    rows = bondization["amortizations"]
    with_maturity = rows + [
        {"amortdate": "2035-06-26", "value": 500.0, "data_source": "maturity"}
    ]
    payments = amortization_payments({"amortizations": with_maturity}, 1000.0, date(2026, 10, 26))
    total = sum(p.share_of_nominal for p in payments)
    assert total <= 1.0
    assert all(p.share_of_nominal > 0 for p in payments)


def test_amortization_uses_initial_nominal(bondization):
    """Доли считаются от исходного номинала, а не от текущего."""
    rows = bondization["amortizations"]
    first_value = rows[0]["value"]

    from_initial = amortization_payments(
        {"amortizations": [rows[0]]}, initial_nominal=1000.0, as_of=date(2026, 1, 1)
    )
    from_current = amortization_payments(
        {"amortizations": [rows[0]]}, initial_nominal=862.98, as_of=date(2026, 1, 1)
    )
    assert from_initial[0].share_of_nominal == pytest.approx(first_value / 1000.0)
    assert from_current[0].share_of_nominal > from_initial[0].share_of_nominal


def test_past_amortization_excluded(bondization):
    rows = bondization["amortizations"]
    as_of = date(2026, 10, 26)
    payments = amortization_payments({"amortizations": rows}, 1000.0, as_of)
    assert all(p.payment_date > as_of for p in payments)


# ---------- купоны ----------


def test_past_coupons_excluded(bondization):
    as_of = date(2026, 10, 26)
    schedule = future_coupons(bondization, as_of)
    assert all(d > as_of for d, _ in schedule)
    assert all(v > 0 for _, v in schedule)


def test_coupon_schedule_is_sorted(bondization):
    schedule = future_coupons(bondization, date(2026, 1, 1))
    assert schedule == sorted(schedule)


# ---------- сборка выпуска ----------


def test_build_issue_from_real_data(description, bondization, cfg):
    row = {
        "SECID": "RU000A10FEP7",
        "PREVPRICE": 99.89,
        "ACCRUEDINT": 0.75,
        "LOTSIZE": 1,
        "VALTODAY": 269_127_388,
    }
    issue = build_issue(row, description, bondization, date(2026, 10, 26), {}, cfg)
    assert issue is not None
    assert issue.isin == "RU000A10FEP7"
    assert issue.nominal == pytest.approx(862.98)        # текущий
    assert issue.initial_nominal == pytest.approx(1000.0)  # исходный
    assert issue.coupon_type == "FIXED"
    assert issue.maturity_date == date(2035, 6, 26)


def test_price_applies_to_current_nominal(description, bondization, cfg):
    row = {"SECID": "RU000A10FEP7", "PREVPRICE": 99.89, "ACCRUEDINT": 0.75, "LOTSIZE": 1}
    issue = build_issue(row, description, bondization, date(2026, 10, 26), {}, cfg)
    # 99.89% от 862.98 + НКД 0.75 = 868.50, а не 999 от исходной 1000
    cost = (issue.clean_price + issue.accrued_interest) / 100.0 * issue.nominal
    assert cost == pytest.approx(868.50, abs=0.01)
    assert cost < 900  # ключевое: не 1000


def test_foreign_currency_rejected(description, bondization, cfg):
    desc = dict(description, FACEUNIT="USD")
    issue = build_issue({"SECID": "X", "PREVPRICE": 100.0}, desc, bondization, date(2026, 10, 26), {}, cfg)
    assert issue is None


def test_expired_issue_rejected(description, bondization, cfg):
    issue = build_issue(
        {"SECID": "X", "PREVPRICE": 100.0}, description, bondization, date(2040, 1, 1), {}, cfg
    )
    assert issue is None


def test_missing_rating_leaves_none(description, bondization, cfg):
    row = {"SECID": "RU000A10FEP7", "PREVPRICE": 99.89, "ACCRUEDINT": 0.75, "LOTSIZE": 1}
    issue = build_issue(row, description, bondization, date(2026, 10, 26), {}, cfg)
    assert issue.rating is None
    assert issue.quality is Quality.MEDIUM


def test_rating_from_directory(description, bondization, cfg):
    from bond_strategy.ingestion.mapping import IssuerRecord

    issuers = {
        "15518": IssuerRecord(
            emitter_id="15518",
            name="Тестовый эмитент",
            industry="Секьюритизация",
            rating="A+",
            rating_agency="АКРА",
            rating_date=date(2026, 6, 1),
        )
    }
    row = {"SECID": "RU000A10FEP7", "PREVPRICE": 99.89, "ACCRUEDINT": 0.75, "LOTSIZE": 1}
    issue = build_issue(row, description, bondization, date(2026, 10, 26), issuers, cfg)
    assert issue.rating == "A+"
    assert issue.quality is Quality.HIGH
    assert issue.issuer_name == "Тестовый эмитент"


def test_empty_issuer_directory_is_valid():
    assert load_issuers("config/issuers.json") == {}


def test_quality_without_issuer_is_medium():
    assert quality_for(None) is Quality.MEDIUM


# ---------- дата цены ----------


def test_price_date_from_prevdate_not_systime(description, bondization, cfg):
    """SYSTIME — время ответа сервера, а не дата цены.

    Раньше price_date брался из SYSTIME. При расчёте на прошлую дату это
    давало отрицательный возраст цены и статус STALE у свежих данных.
    """
    row = {
        "SECID": "RU000A10FEP7",
        "PREVPRICE": 99.89,
        "ACCRUEDINT": 0.75,
        "LOTSIZE": 1,
        "PREVDATE": "2026-10-23",
        "SYSTIME": "2026-10-27 08:15:00",
    }
    issue = build_issue(row, description, bondization, date(2026, 10, 26), {}, cfg)
    assert issue.price_date == date(2026, 10, 23)
    assert issue.status["price"] is DataStatus.FRESH


def test_price_date_tradedate_wins_over_prevdate(description, bondization, cfg):
    """TRADEDATE точнее: если ISS его отдал, берётся он."""
    row = {
        "SECID": "RU000A10FEP7",
        "PREVPRICE": 99.89,
        "ACCRUEDINT": 0.75,
        "LOTSIZE": 1,
        "TRADEDATE": "2026-10-26",
        "PREVDATE": "2026-10-23",
    }
    issue = build_issue(row, description, bondization, date(2026, 10, 26), {}, cfg)
    assert issue.price_date == date(2026, 10, 26)


def test_missing_price_date_is_unverified_not_stale(description, bondization, cfg):
    """Без даты цены статус UNVERIFIED, а не STALE и не свежий."""
    row = {"SECID": "RU000A10FEP7", "PREVPRICE": 99.89, "ACCRUEDINT": 0.75, "LOTSIZE": 1}
    issue = build_issue(row, description, bondization, date(2026, 10, 26), {}, cfg)
    assert issue.price_date is None
    assert issue.status["price"] is DataStatus.UNVERIFIED
    assert issue.status["price"].blocks_buy is True


def test_old_price_is_stale(description, bondization, cfg):
    """Цена старше допуска действительно STALE — проверка не сломана."""
    row = {
        "SECID": "RU000A10FEP7",
        "PREVPRICE": 99.89,
        "ACCRUEDINT": 0.75,
        "LOTSIZE": 1,
        "PREVDATE": "2026-09-01",
    }
    issue = build_issue(row, description, bondization, date(2026, 10, 26), {}, cfg)
    assert issue.status["price"] is DataStatus.STALE
