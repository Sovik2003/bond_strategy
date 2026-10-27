"""Синтетический набор данных для проверки аналитического ядра.

Данные не относятся к реальным эмитентам и предназначены только для тестов.
"""
from __future__ import annotations

from datetime import date

from .models import AmortizationPayment, BondIssue, DataStatus, Quality


def _fresh(**fields: DataStatus) -> dict[str, DataStatus]:
    return dict(fields)


def normal_issue() -> BondIssue:
    """Обычный выпуск: должен проходить все фильтры."""
    return BondIssue(
        isin="RU000TEST0001",
        ticker="TEST1",
        issuer_id="ISS_A",
        issuer_name="Тестовый эмитент А",
        industry="Промышленность",
        nominal=1000.0,
        coupon_type="FIXED",
        coupon_rate=0.16,
        coupon_frequency=2,
        maturity_date=date(2028, 6, 1),
        rating="A+",
        rating_agency="Agency1",
        rating_date=date(2026, 6, 1),
        quality=Quality.HIGH,
        clean_price=98.5,
        accrued_interest=7.5,
        price_date=date(2026, 10, 23),
        avg_daily_turnover=2_000_000.0,
        avg_spread=0.003,
        ofz_ytm=0.135,
        net_debt_to_ebitda=2.5,
        interest_coverage=4.0,
        status=_fresh(
            price=DataStatus.FRESH,
            accrued_interest=DataStatus.FRESH,
            coupon=DataStatus.FRESH,
            maturity=DataStatus.FRESH,
            rating=DataStatus.FRESH,
            availability=DataStatus.FRESH,
            default=DataStatus.FRESH,
            liquidity=DataStatus.FRESH,
        ),
    )


def amortizing_issue() -> BondIssue:
    b = normal_issue()
    b.isin = "RU000TEST0002"
    b.ticker = "TEST2"
    b.issuer_id = "ISS_B"
    b.issuer_name = "Тестовый эмитент Б"
    b.amortization = [
        AmortizationPayment(date(2027, 6, 1), 0.25),
        AmortizationPayment(date(2027, 12, 1), 0.25),
    ]
    b.maturity_date = date(2028, 6, 1)
    b.clean_price = 99.0
    return b


def offer_issue() -> BondIssue:
    b = normal_issue()
    b.isin = "RU000TEST0003"
    b.ticker = "TEST3"
    b.offer_flag = True
    return b


def floating_issue() -> BondIssue:
    b = normal_issue()
    b.isin = "RU000TEST0004"
    b.ticker = "TEST4"
    b.coupon_type = "FLOATING"
    return b


def default_issue() -> BondIssue:
    b = normal_issue()
    b.isin = "RU000TEST0005"
    b.ticker = "TEST5"
    b.default_flag = True
    return b


def stale_price_issue() -> BondIssue:
    b = normal_issue()
    b.isin = "RU000TEST0006"
    b.ticker = "TEST6"
    b.issuer_id = "ISS_C"
    b.status = dict(b.status)
    b.status["price"] = DataStatus.STALE
    return b


def conflicting_rating_issue() -> BondIssue:
    b = normal_issue()
    b.isin = "RU000TEST0007"
    b.ticker = "TEST7"
    b.issuer_id = "ISS_D"
    b.status = dict(b.status)
    b.status["rating"] = DataStatus.CONFLICT
    return b


def illiquid_issue() -> BondIssue:
    b = normal_issue()
    b.isin = "RU000TEST0008"
    b.ticker = "TEST8"
    b.issuer_id = "ISS_E"
    b.avg_daily_turnover = 50_000.0
    b.avg_spread = 0.02
    return b


def medium_quality_issue() -> BondIssue:
    b = normal_issue()
    b.isin = "RU000TEST0009"
    b.ticker = "TEST9"
    b.issuer_id = "ISS_F"
    b.issuer_name = "Тестовый эмитент Ф"
    b.industry = "Финансы"
    b.rating = "BBB-"
    b.quality = Quality.MEDIUM
    b.clean_price = 96.0
    b.ofz_ytm = 0.135
    return b


def all_fixtures() -> list[BondIssue]:
    return [
        normal_issue(),
        amortizing_issue(),
        offer_issue(),
        floating_issue(),
        default_issue(),
        stale_price_issue(),
        conflicting_rating_issue(),
        illiquid_issue(),
        medium_quality_issue(),
    ]


TEST_DATE = date(2026, 10, 26)
