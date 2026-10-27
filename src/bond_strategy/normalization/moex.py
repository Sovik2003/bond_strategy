"""Нормализация данных MOEX ISS в модель BondIssue."""
from __future__ import annotations

from datetime import date, datetime

from ..models import AmortizationPayment, BondIssue, DataStatus, Quality


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def normalize_bond_type(bond_type: str | None) -> str:
    """Преобразование BOND_TYPE в FIXED или FLOATING."""
    if not bond_type:
        return "UNKNOWN"
    s = bond_type.lower()
    if "плавающ" in s or "переменн" in s:
        return "FLOATING"
    if "фиксированн" in s or "известн" in s:
        return "FIXED"
    if "валют" in s:
        return "FOREIGN"
    return "UNKNOWN"


def parse_quality_from_listlevel(listlevel: str | int | None) -> Quality:
    """Грубый прокси рейтинга: 1 = HIGH, 2–3 = MEDIUM.

    ISS не даёт рейтингов, только уровень листинга. Это временное решение.
    """
    if listlevel is None:
        return Quality.MEDIUM
    try:
        level = int(listlevel)
        return Quality.HIGH if level == 1 else Quality.MEDIUM
    except (ValueError, TypeError):
        return Quality.MEDIUM


def moex_to_bond_issue(
    security: dict,
    description: dict,
    bondization: dict,
    marketdata: dict | None,
    as_of: date,
) -> BondIssue | None:
    """Сборка BondIssue из сырых данных MOEX ISS.

    Возвращает None, если выпуск не подходит (валютный, нет номинала и т.д.).
    """
    secid = security.get("SECID")
    if not secid:
        return None

    # Валюта номинала
    faceunit = description.get("FACEUNIT")
    if faceunit != "SUR":  # только рубли
        return None

    # Тип купона
    bond_type_raw = description.get("BOND_TYPE")
    coupon_type = normalize_bond_type(bond_type_raw)
    if coupon_type not in ("FIXED", "FLOATING"):
        return None

    # Номинал
    try:
        nominal = float(description.get("FACEVALUE") or security.get("FACEVALUE") or 0)
    except (ValueError, TypeError):
        nominal = 0.0
    if nominal <= 0:
        return None

    # Даты
    maturity_date = parse_date(description.get("MATDATE") or security.get("MATDATE"))
    if not maturity_date or maturity_date <= as_of:
        return None

    # Купон
    coupon_rate_pct = description.get("COUPONPERCENT") or security.get("COUPONPERCENT")
    coupon_rate = float(coupon_rate_pct) / 100.0 if coupon_rate_pct else None

    coupon_frequency = description.get("COUPONFREQUENCY")
    try:
        freq = int(coupon_frequency) if coupon_frequency else 0
    except (ValueError, TypeError):
        freq = 0

    # Оферта
    offers = bondization.get("offers", [])
    offer_flag = len(offers) > 0

    # Амортизация
    amorts = bondization.get("amortizations", [])
    amortization_schedule = []
    for a in amorts:
        if a.get("data_source") == "maturity":
            continue  # это погашение, не амортизация
        amt_date = parse_date(a.get("amortdate"))
        value_prc = a.get("valueprc")
        if amt_date and value_prc is not None:
            try:
                share = float(value_prc) / 100.0
                amortization_schedule.append(AmortizationPayment(amt_date, share))
            except (ValueError, TypeError):
                pass

    # Квалифицированный инвестор
    qualified = description.get("ISQUALIFIEDINVESTORS")
    qualified_only = qualified == "1" if qualified else False

    # Дефолт
    has_default = description.get("HASDEFAULT") == 1
    has_tech_default = description.get("HASTECHNICALDEFAULT") == 1
    default_flag = has_default or has_tech_default

    # Минимальный лот
    try:
        min_lot = int(security.get("LOTSIZE") or 1)
    except (ValueError, TypeError):
        min_lot = 1

    # Качество (временный прокси)
    listlevel = description.get("LISTLEVEL") or security.get("LISTLEVEL")
    quality = parse_quality_from_listlevel(listlevel)

    # Эмитент
    emitter_id = description.get("EMITTER_ID")
    issuer_id = str(emitter_id) if emitter_id else secid
    issuer_name = description.get("ISSUENAME") or security.get("SHORTNAME") or secid

    # Рыночные данные
    clean_price = None
    accrued_interest = None
    avg_daily_turnover = None
    avg_spread = None
    ytm = None
    price_date = None

    if marketdata:
        # Цена: PREVWAPRICE или LAST
        clean_price = marketdata.get("PREVWAPRICE") or marketdata.get("LAST")
        accrued_interest = marketdata.get("ACCRUEDINT")
        ytm = marketdata.get("YIELDATWAPRICE") or marketdata.get("YIELD")

        # Спред
        bid = marketdata.get("BID")
        offer = marketdata.get("OFFER")
        if bid and offer and bid > 0 and offer > 0:
            mid = (bid + offer) / 2.0
            avg_spread = (offer - bid) / mid if mid > 0 else None

        # Оборот сегодня
        valtoday = marketdata.get("VALTODAY")
        try:
            if valtoday is not None and float(valtoday) > 0:
                avg_daily_turnover = float(valtoday)
        except (ValueError, TypeError):
            pass

        price_date = as_of

    # Статусы данных
    status = {
        "price": DataStatus.FRESH if clean_price is not None else DataStatus.MISSING,
        "accrued_interest": DataStatus.FRESH if accrued_interest is not None else DataStatus.MISSING,
        "coupon": DataStatus.FRESH if coupon_rate is not None else DataStatus.MISSING,
        "maturity": DataStatus.FRESH if maturity_date is not None else DataStatus.MISSING,
        "rating": DataStatus.MISSING,  # ISS не даёт рейтингов
        "availability": DataStatus.FRESH,  # ISQUALIFIEDINVESTORS известен
        "default": DataStatus.FRESH,  # HASDEFAULT известен
        "liquidity": DataStatus.FRESH if avg_daily_turnover is not None else DataStatus.MISSING,
    }

    return BondIssue(
        isin=description.get("ISIN") or secid,
        ticker=secid,
        issuer_id=issuer_id,
        issuer_name=issuer_name,
        industry="Неизвестно",  # ISS не даёт отрасль
        nominal=nominal,
        coupon_type=coupon_type,
        coupon_rate=coupon_rate,
        coupon_frequency=freq,
        maturity_date=maturity_date,
        offer_flag=offer_flag,
        amortization=amortization_schedule,
        qualified_only=qualified_only,
        min_lot=min_lot,
        quality=quality,
        rating=None,  # ISS не даёт рейтинг
        rating_agency=None,
        rating_date=None,
        default_flag=default_flag,
        restructuring_flag=False,  # ISS не даёт явный признак реструктуризации
        reporting_sufficient=True,  # пока допущение
        clean_price=clean_price,
        accrued_interest=accrued_interest,
        price_date=price_date,
        avg_daily_turnover=avg_daily_turnover,
        avg_spread=avg_spread,
        ytm=ytm,
        ofz_ytm=None,  # устанавливается отдельно после загрузки ОФЗ
        status=status,
    )
