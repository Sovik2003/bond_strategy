"""Синтетические выпуски для тестов. Не относятся к реальным эмитентам."""
from __future__ import annotations

from datetime import date

from bond_strategy.models import AmortizationPayment, BondIssue, DataStatus, Quality

TEST_DATE = date(2026, 10, 26)


def _all_fresh() -> dict[str, DataStatus]:
    return {
        "price": DataStatus.FRESH,
        "accrued_interest": DataStatus.FRESH,
        "coupon": DataStatus.FRESH,
        "maturity": DataStatus.FRESH,
        "liquidity": DataStatus.FRESH,
        "default": DataStatus.FRESH,
        "rating": DataStatus.FRESH,
    }


def normal() -> BondIssue:
    return BondIssue(
        isin="RU000TEST0001",
        secid="TEST1",
        issuer_id="ISS_A",
        issuer_name="Эмитент А",
        industry="Промышленность",
        nominal=1000.0,
        coupon_type="FIXED",
        coupon_rate=0.16,
        coupon_frequency=2,
        maturity_date=date(2028, 6, 1),
        rating="A+",
        rating_agency="АКРА",
        rating_date=date(2026, 6, 1),
        quality=Quality.HIGH,
        list_level=1,
        clean_price=98.5,
        accrued_interest=7.5,
        price_date=date(2026, 10, 23),
        avg_daily_turnover=2_000_000.0,
        avg_spread=0.002,
        ofz_ytm=0.135,
        status=_all_fresh(),
    )


def amortizing() -> BondIssue:
    b = normal()
    b.isin, b.secid, b.issuer_id = "RU000TEST0002", "TEST2", "ISS_B"
    b.issuer_name = "Эмитент Б"
    b.amortization = [
        AmortizationPayment(date(2027, 6, 1), 0.25),
        AmortizationPayment(date(2027, 12, 1), 0.25),
    ]
    return b


def with_offer() -> BondIssue:
    b = normal()
    b.isin, b.secid, b.issuer_id = "RU000TEST0003", "TEST3", "ISS_C"
    b.offer_date = date(2027, 3, 1)
    return b


def floating() -> BondIssue:
    b = normal()
    b.isin, b.secid, b.issuer_id = "RU000TEST0004", "TEST4", "ISS_D"
    b.coupon_type = "FLOATING"
    return b


def defaulted() -> BondIssue:
    b = normal()
    b.isin, b.secid, b.issuer_id = "RU000TEST0005", "TEST5", "ISS_E"
    b.default_flag = True
    return b


def no_price() -> BondIssue:
    b = normal()
    b.isin, b.secid, b.issuer_id = "RU000TEST0006", "TEST6", "ISS_F"
    b.clean_price = None
    b.status = dict(b.status, price=DataStatus.MISSING, accrued_interest=DataStatus.MISSING)
    return b


def stale_price() -> BondIssue:
    b = normal()
    b.isin, b.secid, b.issuer_id = "RU000TEST0007", "TEST7", "ISS_G"
    b.price_date = date(2026, 9, 1)
    b.status = dict(b.status, price=DataStatus.STALE, accrued_interest=DataStatus.STALE)
    return b


def conflicting_rating() -> BondIssue:
    b = normal()
    b.isin, b.secid, b.issuer_id = "RU000TEST0008", "TEST8", "ISS_H"
    b.status = dict(b.status, rating=DataStatus.CONFLICT)
    return b


def illiquid() -> BondIssue:
    b = normal()
    b.isin, b.secid, b.issuer_id = "RU000TEST0009", "TEST9", "ISS_I"
    b.avg_daily_turnover = 50_000.0
    b.avg_spread = 0.02
    return b


def medium_quality() -> BondIssue:
    b = normal()
    b.isin, b.secid, b.issuer_id = "RU000TEST0010", "TEST10", "ISS_J"
    b.issuer_name = "Эмитент Ж"
    b.industry = "Финансы"
    b.rating = "BBB-"
    b.quality = Quality.MEDIUM
    b.clean_price = 96.0
    return b


def no_rating() -> BondIssue:
    b = normal()
    b.isin, b.secid, b.issuer_id = "RU000TEST0011", "TEST11", "ISS_K"
    b.rating = None
    b.rating_date = None
    return b


def all_issues() -> list[BondIssue]:
    return [
        normal(), amortizing(), with_offer(), floating(), defaulted(),
        no_price(), stale_price(), conflicting_rating(), illiquid(),
        medium_quality(), no_rating(),
    ]
