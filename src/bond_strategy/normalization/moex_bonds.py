"""Преобразование данных MOEX ISS в доменные модели.

Границы ответственности:
- здесь только разбор и приведение типов;
- жёсткие правила стратегии (дефолт, оферта, ликвидность) — в signals/filters.py;
- рейтинг и отрасль из ISS не приходят, поэтому они помечаются как
  отсутствующие, а не подставляются догадками.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from ..models import AmortizationPayment, BondIssue, DataStatus, Quality

RUBLE = "SUR"

# Типы выпусков по BOND_TYPE. Сравнение по подстроке, а не по точному тексту:
# формулировки ISS меняются, а смысл остаётся.
FIXED_COUPON_MARKERS = ("фиксированным", "постоянным", "амортизируем")
FLOATING_COUPON_MARKERS = ("плавающим", "переменным", "индексируемым")
FX_MARKERS = ("валютная", "валютн")


def _to_float(value: Any) -> float | None:
    if value in (None, "", "None"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_date(value: Any) -> date | None:
    if not value or value in ("0000-00-00", "None"):
        return None
    text = str(value)[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def classify_coupon_type(bond_type: str | None) -> str:
    """FIXED / FLOATING / FX / OTHER по описанию ISS."""
    text = (bond_type or "").lower()
    if any(marker in text for marker in FX_MARKERS):
        return "FX"
    if any(marker in text for marker in FLOATING_COUPON_MARKERS):
        return "FLOATING"
    if any(marker in text for marker in FIXED_COUPON_MARKERS):
        return "FIXED"
    return "OTHER"


@dataclass
class NormalizationReport:
    """Что именно не удалось получить. Без этого отчёт неполон."""

    missing_rating: list[str] = field(default_factory=list)
    missing_industry: list[str] = field(default_factory=list)
    floating_coupon: int = 0
    fx_issues: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)

    def note_skip(self, secid: str, reason: str) -> None:
        self.skipped.append((secid, reason))


def build_amortization_schedule(
    amortization_rows: list[dict[str, Any]],
    initial_nominal: float,
    settlement: date,
) -> list[AmortizationPayment]:
    """График амортизации как доли от первоначального номинала.

    ISS отдаёт facevalue — остаток номинала после выплаты, а не сумму выплаты.
    Строка с data_source='maturity' — это погашение, а не амортизация:
    её обрабатывает отдельный механизм redemption в портфеле.
    """
    payments: list[AmortizationPayment] = []
    previous = initial_nominal

    for row in sorted(
        (r for r in amortization_rows if r.get("data_source") != "maturity"),
        key=lambda r: r.get("amortdate") or "",
    ):
        pay_date = _to_date(row.get("amortdate"))
        remaining = _to_float(row.get("facevalue"))
        if pay_date is None or remaining is None or initial_nominal <= 0:
            continue
        if pay_date <= settlement:
            previous = remaining
            continue
        paid = previous - remaining
        previous = remaining
        if paid <= 0:
            continue
        share = paid / initial_nominal
        if share > 1:
            share = 1.0
        payments.append(AmortizationPayment(pay_date, round(share, 6)))

    return payments


def coupon_is_known(coupon_rows: list[dict[str, Any]], settlement: date) -> bool:
    """Все будущие купоны должны быть известны.

    У бумаг с плавающим купоном ISS оставляет value=None на будущие даты —
    это надёжный признак того, что денежный поток непредсказуем.
    """
    future = [
        r for r in coupon_rows
        if _to_date(r.get("coupondate")) and _to_date(r["coupondate"]) > settlement
    ]
    if not future:
        return False
    return all(_to_float(r.get("value")) is not None for r in future)


def effective_coupon_rate(
    coupon_rows: list[dict[str, Any]],
    settlement: date,
    nominal: float,
    frequency: int,
) -> float | None:
    """Годовая ставка по ближайшему будущему купону.

    Считается как value / номинал * частота, а не берётся из COUPONPERCENT:
    при амортизации процент от первоначального номинала вводит в заблуждение.
    """
    future = sorted(
        (
            r for r in coupon_rows
            if _to_date(r.get("coupondate")) and _to_date(r["coupondate"]) > settlement
        ),
        key=lambda r: r["coupondate"],
    )
    if not future or frequency <= 0 or nominal <= 0:
        return None
    value = _to_float(future[0].get("value"))
    if value is None:
        return None
    return value * frequency / nominal


def average_turnover(history_rows: list[dict[str, Any]], days: int = 20) -> float | None:
    """Средний дневной оборот по последним торговым дням.

    Берётся только основной режим: сделки в иных режимах не подтверждают
    ликвидность на доступном рынке.
    """
    values = [
        _to_float(r.get("VALUE")) or 0.0
        for r in history_rows
        if r.get("BOARDID") in (None, "TQCB")
    ]
    if not values:
        return None
    tail = values[-days:]
    return sum(tail) / len(tail)


def estimate_spread(market: dict[str, Any]) -> float | None:
    """Относительный спред по лучшим ценам. None, если одной из сторон нет."""
    bid = _to_float(market.get("BID"))
    offer = _to_float(market.get("OFFER"))
    if not bid or not offer or bid <= 0 or offer <= 0:
        return None
    mid = (bid + offer) / 2.0
    if mid <= 0:
        return None
    return (offer - bid) / mid


def build_bond_issue(
    security: dict[str, Any],
    market: dict[str, Any],
    description: dict[str, str],
    bondization: dict[str, list[dict[str, Any]]],
    history: list[dict[str, Any]],
    ofz_yield: float | None,
    settlement: date,
    report: NormalizationReport,
) -> BondIssue | None:
    """Собирает BondIssue. Возвращает None, если выпуск непригоден для расчёта."""
    secid = security["SECID"]
    coupon_type = classify_coupon_type(description.get("BOND_TYPE"))
    # Валюта проверяется отдельно: текст типа выпуска не всегда её содержит
    face_unit = description.get("FACEUNIT") or security.get("FACEUNIT")
    if coupon_type == "FX" or (face_unit and face_unit != RUBLE):
        report.fx_issues += 1
        report.note_skip(secid, f"валютный выпуск ({face_unit})")
        return None
    if coupon_type == "FLOATING":
        report.floating_coupon += 1
        report.note_skip(secid, "плавающий купон")
        return None
    if coupon_type != "FIXED":
        report.note_skip(secid, f"неизвестный тип купона: {description.get('BOND_TYPE')!r}")
        return None

    coupons = bondization.get("coupons") or []
    if not coupon_is_known(coupons, settlement):
        report.note_skip(secid, "будущие купоны неизвестны")
        return None

    nominal = _to_float(description.get("FACEVALUE")) or _to_float(security.get("FACEVALUE")) or 1000.0
    initial_nominal = _to_float(description.get("INITIALFACEVALUE")) or nominal
    frequency = int(_to_float(description.get("COUPONFREQUENCY")) or 0) or 2

    rate = effective_coupon_rate(coupons, settlement, nominal, frequency)
    maturity = _to_date(description.get("MATDATE")) or _to_date(security.get("MATDATE"))

    clean_price = _to_float(market.get("LAST")) or _to_float(market.get("WAPRICE")) \
        or _to_float(security.get("PREVPRICE"))
    accrued = _to_float(security.get("ACCRUEDINT"))
    if accrued is None:
        accrued = _to_float(market.get("ACCRUEDINT"))

    turnover = average_turnover(history) or _to_float(market.get("VALTODAY"))
    spread = estimate_spread(market)

    offers = bondization.get("offers") or []
    has_future_offer = any(
        _to_date(o.get("offerdate")) and _to_date(o["offerdate"]) > settlement
        for o in offers
    )

    amortization = build_amortization_schedule(
        bondization.get("amortizations") or [], initial_nominal, settlement
    )

    emitter_id = description.get("EMITTER_ID") or ""
    has_default = str(description.get("HASDEFAULT")) == "1"
    has_tech_default = str(description.get("HASTECHNICALDEFAULT")) == "1"

    # Рейтинга и отрасли в ISS нет: фиксируем именно это, а не выдумываем значения
    report.missing_rating.append(secid)
    report.missing_industry.append(secid)

    status: dict[str, DataStatus] = {
        "price": DataStatus.FRESH if clean_price is not None else DataStatus.MISSING,
        "accrued_interest": DataStatus.FRESH if accrued is not None else DataStatus.MISSING,
        "coupon": DataStatus.FRESH if rate is not None else DataStatus.MISSING,
        "maturity": DataStatus.FRESH if maturity is not None else DataStatus.MISSING,
        "offer": DataStatus.FRESH,
        "default": DataStatus.FRESH if not has_default else DataStatus.CONFLICT,
        "liquidity": DataStatus.FRESH if (turnover is not None and spread is not None) else DataStatus.MISSING,
        # Отрасль из ISS не приходит — это известный пробел, а не свежие данные
        "industry": DataStatus.MISSING,
        # Рейтинг отсутствует: без него жёсткий фильтр не пропустит выпуск
        "rating": DataStatus.MISSING,
        "availability": (
            DataStatus.FRESH
            if str(description.get("ISQUALIFIEDINVESTORS")) in ("0", "")
            else DataStatus.FRESH
        ),
    }

    return BondIssue(
        isin=description.get("ISIN") or secid,
        ticker=secid,
        issuer_id=emitter_id or f"UNKNOWN_{secid}",
        issuer_name=f"EMITTER_{emitter_id}" if emitter_id else "Неизвестный эмитент",
        industry="UNKNOWN",
        nominal=nominal,
        coupon_type="FIXED",
        coupon_rate=rate,
        coupon_frequency=frequency,
        maturity_date=maturity,
        offer_flag=has_future_offer,
        amortization=amortization,
        qualified_only=str(description.get("ISQUALIFIEDINVESTORS")) == "1",
        min_lot=int(_to_float(security.get("LOTSIZE")) or 1),
        quality=Quality.HIGH,
        rating=None,
        rating_agency=None,
        rating_date=None,
        default_flag=has_default or has_tech_default,
        restructuring_flag=False,
        reporting_sufficient=False,
        clean_price=clean_price,
        accrued_interest=accrued,
        price_date=settlement,
        avg_daily_turnover=turnover,
        avg_spread=spread,
        ofz_ytm=ofz_yield,
        status=status,
    )
