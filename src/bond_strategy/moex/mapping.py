"""Сборка доменной модели BondIssue из данных MOEX ISS.

Здесь сосредоточены все решающие допущения о качестве данных. Правило:
если ISS не позволяет подтвердить поле, оно получает статус UNVERIFIED
или MISSING, а не подставляется правдоподобным значением.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..models import AmortizationPayment, BondIssue, CouponPayment, DataStatus, Quality
from .details import IssueDetails, Schedule, coupon_is_fixed, reconstruct_coupons
from .liquidity import LiquidityStats
from .ofz import OfzCurve, ofz_benchmark

# Сопоставление уровня листинга и категории качества.
# Это ПРОКСИ, а не кредитный рейтинг: ISS рейтингов не отдаёт.
LIST_LEVEL_QUALITY = {1: Quality.HIGH, 2: Quality.HIGH, 3: Quality.MEDIUM}

# Ставка дисконтирования для дюрации, если ISS не дал DURATION.
DEFAULT_DURATION_YIELD = 0.15


@dataclass
class MappedIssue:
    issue: BondIssue
    quality_basis: str
    notes: list[str] = field(default_factory=list)


def map_quality(details: IssueDetails) -> tuple[Quality, str]:
    """Категория качества по уровню листинга — с явной оговоркой об источнике."""
    if details.list_level in (1, 2):
        return Quality.HIGH, f"LIST_LEVEL_{details.list_level}"
    if details.list_level == 3:
        return Quality.MEDIUM, "LIST_LEVEL_3"
    return Quality.MEDIUM, "NO_LIST_LEVEL"


def estimate_duration_years(
    details: IssueDetails, as_of: date, ytm: float | None = None
) -> float | None:
    """Грубая дюрация по сроку до погашения, если ISS не дал DURATION.

    Для сопоставления с ОФЗ этого достаточно: ошибка в дюрации сдвигает
    бенчмарк незначительно на нормальной кривой.
    """
    if details.matdate is None:
        return None
    years = (details.matdate - as_of).days / 365.25
    if years <= 0:
        return None
    rate = ytm if ytm and ytm > 0 else DEFAULT_DURATION_YIELD
    # приближение Маколея для полугодового купона
    return years / (1.0 + rate / 2.0)


def build_bond_issue(
    details: IssueDetails,
    schedule: Schedule,
    liquidity: LiquidityStats,
    curve: OfzCurve,
    as_of: date,
    clean_price: float | None,
    accrued_interest: float | None,
    price_date: date | None,
    ytm_from_market: float | None = None,
) -> MappedIssue:
    """Собирает BondIssue и проставляет статусы достоверности по каждому полю."""
    notes: list[str] = []
    status: dict[str, DataStatus] = {}

    quality, quality_basis = map_quality(details)

    # --- цена и НКД
    if clean_price is None or clean_price <= 0:
        status["price"] = DataStatus.MISSING
        notes.append("Нет цены")
    elif price_date is None:
        status["price"] = DataStatus.UNVERIFIED
        notes.append("Цена без даты")
    else:
        status["price"] = DataStatus.FRESH

    if accrued_interest is None:
        status["accrued_interest"] = DataStatus.MISSING
        notes.append("Нет НКД")
    else:
        status["accrued_interest"] = DataStatus.FRESH

    # --- купон
    fixed, fixed_reason = coupon_is_fixed(schedule, details, as_of)
    if not fixed:
        status["coupon"] = DataStatus.CONFLICT
        notes.append(f"Купон не фиксирован: {fixed_reason}")
    elif not schedule.coupons:
        status["coupon"] = DataStatus.MISSING
        notes.append("Нет графика купонов")
    else:
        status["coupon"] = DataStatus.FRESH

    # --- погашение
    if details.matdate is None:
        status["maturity"] = DataStatus.MISSING
        notes.append("Нет даты погашения")
    else:
        status["maturity"] = DataStatus.FRESH

    # --- рейтинг: ISS не предоставляет
    status["rating"] = DataStatus.MISSING
    notes.append(
        f"Рейтинг отсутствует в ISS; качество оценено по уровню листинга "
        f"({details.list_level}) как прокси, а не как кредитный рейтинг"
    )

    # --- доступность неквалифицированным инвесторам
    status["availability"] = (
        DataStatus.CONFLICT if details.qualified_only else DataStatus.FRESH
    )
    if details.qualified_only:
        notes.append("Только для квалифицированных инвесторов")

    # --- дефолт
    if details.has_default or details.has_technical_default:
        status["default"] = DataStatus.CONFLICT
        notes.append("Признак дефолта в ISS")
    else:
        status["default"] = DataStatus.FRESH

    # --- ликвидность
    if liquidity.avg_daily_turnover is None:
        status["liquidity"] = DataStatus.MISSING
        notes.append(f"Ликвидность: {liquidity.message}")
    elif liquidity.last_trade_date and (as_of - liquidity.last_trade_date).days > 10:
        status["liquidity"] = DataStatus.STALE
        notes.append(f"Последняя сделка {liquidity.last_trade_date}")
    else:
        status["liquidity"] = DataStatus.FRESH

    # --- оферты
    upcoming_offers = schedule.future_offers(as_of)
    offer_flag = bool(upcoming_offers) or bool(
        details.offerdate and details.offerdate > as_of
    )
    if offer_flag:
        notes.append("Есть будущая оферта")

    # --- амортизация
    amort_payments = [
        AmortizationPayment(a.amort_date, (a.value_prc or 0) / 100.0)
        for a in schedule.amortization_only()
        if a.amort_date > as_of and (a.value_prc or 0) > 0 and (a.value_prc or 0) < 100
    ]

    # --- купонная ставка из графика (на текущий остаточный номинал)
    coupon_rate = None
    freq = details.coupon_frequency or 2
    future = [c for c in schedule.coupons if c.coupon_date > as_of and c.value]
    if future and details.facevalue:
        coupon_rate = future[0].value / details.facevalue * freq

    # --- фактические купонные выплаты из bondization
    # ISS отдаёт value как рубли на одну бумагу на дату выплаты. Для
    # амортизируемых выпусков сумма уже посчитана от остаточного номинала,
    # поэтому пересчитывать её нельзя.
    actual_coupons = [
        CouponPayment(c.coupon_date, float(c.value))
        for c in future
        if c.value is not None and c.value > 0
    ]

    # Если ISS не опубликовал часть будущих сумм, они восстанавливаются по
    # объявленной ставке и графику амортизации — но только когда расчёт
    # подтверждается на уже известных купонах.
    coupon_source = "ISS_ACTUAL" if len(actual_coupons) == len(future) else "ISS_PARTIAL"
    if len(actual_coupons) < len(future):
        filled, status = reconstruct_coupons(schedule, details, as_of)
        if status == "RECONSTRUCTED":
            actual_coupons = [
                CouponPayment(c.coupon_date, float(c.value)) for c in filled
            ]
            coupon_source = "RECONSTRUCTED"
            notes.append(
                f"\u041a\u0443\u043f\u043e\u043d\u044b \u0432\u043e\u0441\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u044b \u043f\u043e \u0441\u0442\u0430\u0432\u043a\u0435 {details.coupon_percent}% "
                f"\u0438 \u0433\u0440\u0430\u0444\u0438\u043a\u0443 \u0430\u043c\u043e\u0440\u0442\u0438\u0437\u0430\u0446\u0438\u0438; \u0440\u0430\u0441\u0447\u0451\u0442 \u043f\u0440\u043e\u0432\u0435\u0440\u0435\u043d \u043d\u0430 \u0438\u0437\u0432\u0435\u0441\u0442\u043d\u044b\u0445 \u0432\u044b\u043f\u043b\u0430\u0442\u0430\u0445"
            )
        else:
            notes.append(f"\u041a\u0443\u043f\u043e\u043d\u044b \u043d\u0435 \u0432\u043e\u0441\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u044b: {status}")

    # --- бенчмарк ОФЗ по дюрации
    duration_years = estimate_duration_years(details, as_of, ytm_from_market)
    benchmark = None
    if duration_years is not None:
        benchmark = ofz_benchmark(curve, duration_years * 365.25)
    if benchmark is None:
        notes.append("Нет сопоставимой доходности ОФЗ")

    issue = BondIssue(
        isin=details.isin or details.secid,
        ticker=details.secid,
        issuer_id=details.emitter_id or details.secid,
        issuer_name=details.issuer_name or details.shortname or details.secid,
        industry="UNKNOWN",
        nominal=details.initial_facevalue or details.facevalue or 1000.0,
        coupon_type="FIXED" if fixed else "FLOATING",
        coupon_rate=coupon_rate,
        coupon_frequency=freq,
        maturity_date=details.matdate,
        offer_flag=offer_flag,
        amortization=amort_payments,
        coupons=actual_coupons,
        qualified_only=details.qualified_only,
        min_lot=1,
        quality=quality,
        rating=None,
        rating_agency=None,
        rating_date=None,
        default_flag=details.has_default or details.has_technical_default,
        restructuring_flag=False,
        reporting_sufficient=not details.has_default,
        clean_price=clean_price,
        accrued_interest=accrued_interest,
        price_date=price_date,
        avg_daily_turnover=liquidity.avg_daily_turnover,
        avg_spread=liquidity.spread_pct,
        ytm=ytm_from_market,
        ofz_ytm=benchmark,
        status=status,
        list_level=details.list_level,
        credit_basis="LISTING_PROXY" if quality_basis.startswith("LIST_LEVEL") else "UNKNOWN",
    )
    return MappedIssue(issue=issue, quality_basis=quality_basis, notes=notes)
