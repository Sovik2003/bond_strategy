"""Сборка BondIssue из сырых ответов MOEX ISS.

Два номинала, которые нельзя путать:
- FACEVALUE        — текущий остаточный номинал (после прошедшей амортизации),
                     в процентах от него считается цена;
- INITIALFACEVALUE — исходный номинал, от него считаются доли амортизации.

Цена 99.89 при FACEVALUE 862.98 означает 862 ₽, а не 999 ₽. Доли амортизации
считаются от INITIALFACEVALUE, иначе сумма выплат не сойдётся к номиналу.

Рейтинга, отрасли и названия эмитента в ISS нет — они берутся из справочника.
Нет записи в справочнике — rating=None, и выпуск не получит BUY. Это норма.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ..config import StrategyConfig
from ..models import AmortizationPayment, BondIssue, Quality
from ..validation.freshness import apply_validation
from .moex import coupon_type_from, dt, num

SENIOR_RATINGS = {"AAA", "AA+", "AA", "AA-", "A+", "A"}


@dataclass(frozen=True)
class IssuerRecord:
    emitter_id: str
    name: str
    industry: str
    rating: str | None
    rating_agency: str | None
    rating_date: date | None
    reporting_sufficient: bool


def load_issuers(path: str | Path) -> dict[str, IssuerRecord]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    records: dict[str, IssuerRecord] = {}
    for item in data.get("issuers", []):
        eid = str(item["emitter_id"])
        records[eid] = IssuerRecord(
            emitter_id=eid,
            name=item.get("name") or f"Эмитент {eid}",
            industry=item.get("industry") or "Не определена",
            rating=item.get("rating"),
            rating_agency=item.get("rating_agency"),
            rating_date=dt(item.get("rating_date")),
            reporting_sufficient=bool(item.get("reporting_sufficient", False)),
        )
    return records


def future_coupons(
    bondization: dict[str, list[dict]], as_of: date
) -> list[tuple[date, float]]:
    schedule: list[tuple[date, float]] = []
    for row in bondization.get("coupons", []):
        pay_date = dt(row.get("coupondate"))
        value = num(row.get("value"))
        if pay_date and pay_date > as_of and value and value > 0:
            schedule.append((pay_date, value))
    return sorted(schedule)


def amortization_payments(
    bondization: dict[str, list[dict]], initial_nominal: float, as_of: date
) -> list[AmortizationPayment]:
    """Доли амортизации от ИСХОДНОГО номинала.

    Строка data_source='maturity' — это погашение, её учитывает redemption.
    Включение её сюда списало бы номинал дважды.
    """
    payments: list[AmortizationPayment] = []
    if initial_nominal <= 0:
        return payments
    for row in bondization.get("amortizations", []):
        if row.get("data_source") == "maturity":
            continue
        pay_date = dt(row.get("amortdate"))
        value = num(row.get("value"))
        if pay_date and pay_date > as_of and value and value > 0:
            share = value / initial_nominal
            if 0 < share <= 1:
                payments.append(AmortizationPayment(pay_date, share))
    payments.sort(key=lambda p: p.payment_date)
    return payments


def future_offer(bondization: dict[str, list[dict]], as_of: date) -> date | None:
    dates = [
        d
        for d in (dt(r.get("offerdate")) for r in bondization.get("offers", []))
        if d and d > as_of
    ]
    return min(dates) if dates else None


def quality_for(issuer: IssuerRecord | None) -> Quality:
    if issuer is None or issuer.rating is None:
        return Quality.MEDIUM
    return Quality.HIGH if issuer.rating.upper() in SENIOR_RATINGS else Quality.MEDIUM


def build_issue(
    row: dict,
    description: dict[str, str],
    bondization: dict[str, list[dict]],
    as_of: date,
    issuers: dict[str, IssuerRecord],
    cfg: StrategyConfig,
) -> BondIssue | None:
    """None — если бумага заведомо не подходит: валюта или истёкший срок."""
    secid = row["SECID"]

    face_unit = description.get("FACEUNIT") or row.get("FACEUNIT")
    if face_unit not in (None, "SUR", "RUB"):
        return None

    maturity = dt(description.get("MATDATE")) or dt(row.get("MATDATE"))
    if maturity is None or maturity <= as_of:
        return None

    current_nominal = num(description.get("FACEVALUE")) or num(row.get("FACEVALUE")) or 1000.0
    initial_nominal = num(description.get("INITIALFACEVALUE")) or current_nominal

    emitter_id = str(description.get("EMITTER_ID") or "")
    issuer = issuers.get(emitter_id)

    coupon_type = coupon_type_from(
        description.get("BOND_TYPE"),
        description.get("COUPON_BENCHMARK"),
        description.get("COUPON_DETAILS"),
    )

    clean_price = num(row.get("PREVPRICE")) or num(row.get("CLOSEPRICE")) or num(row.get("LAST"))
    nkd = num(row.get("ACCRUEDINT"))
    price_date = dt(row.get("TRADEDATE")) or dt(row.get("SYSTIME"))

    bid, offer = num(row.get("BID")), num(row.get("OFFER"))
    spread = (offer - bid) / ((offer + bid) / 2.0) if (bid and offer and bid > 0 and offer > 0) else None

    coupon_percent = num(description.get("COUPONPERCENT"))

    issue = BondIssue(
        isin=description.get("ISIN") or secid,
        secid=secid,
        issuer_id=emitter_id or secid,
        issuer_name=issuer.name if issuer else f"Эмитент {emitter_id or 'неизвестен'}",
        industry=issuer.industry if issuer else "Не определена",
        nominal=current_nominal,
        initial_nominal=initial_nominal,
        coupon_type=coupon_type,
        coupon_rate=(coupon_percent / 100.0) if coupon_percent else None,
        coupon_frequency=int(num(description.get("COUPONFREQUENCY")) or 2),
        maturity_date=maturity,
        offer_date=future_offer(bondization, as_of),
        amortization=amortization_payments(bondization, initial_nominal, as_of),
        qualified_only=description.get("ISQUALIFIEDINVESTORS") == "1",
        list_level=int(num(description.get("LISTLEVEL")) or 0) or None,
        min_lot=int(num(row.get("LOTSIZE")) or 1),
        quality=quality_for(issuer),
        rating=issuer.rating if issuer else None,
        rating_agency=issuer.rating_agency if issuer else None,
        rating_date=issuer.rating_date if issuer else None,
        reporting_sufficient=issuer.reporting_sufficient if issuer else False,
        clean_price=clean_price,
        accrued_interest=nkd,
        price_date=price_date,
        avg_daily_turnover=num(row.get("VALTODAY")),
        avg_spread=spread,
        coupon_schedule=future_coupons(bondization, as_of),
    )
    return apply_validation(issue, as_of, cfg)
