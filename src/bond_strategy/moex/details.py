"""Детали выпуска и график выплат из MOEX ISS.

Два источника:
  /securities/{secid}.json                                  -> description
  /statistics/.../bondization/{secid}.json                   -> coupons, amortizations, offers

Важное наблюдение по ISS: в блоке amortizations строка с data_source='maturity'
описывает ПОГАШЕНИЕ, а не амортизацию. Если учитывать её как амортизацию,
номинал спишется дважды. Поэтому она трактуется отдельно.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .client import IssClient, block, dt, num

DESCRIPTION_PATH = "/securities/{secid}.json"
BONDIZATION_PATH = "/statistics/engines/stock/markets/bonds/bondization/{secid}.json"


@dataclass
class IssueDetails:
    secid: str
    isin: str | None = None
    name: str | None = None
    shortname: str | None = None
    issuer_name: str | None = None
    emitter_id: str | None = None
    facevalue: float | None = None
    initial_facevalue: float | None = None
    faceunit: str | None = None
    matdate: date | None = None
    offerdate: date | None = None
    coupon_frequency: int | None = None
    coupon_value: float | None = None
    coupon_percent: float | None = None
    bond_type: str | None = None
    bond_subtype: str | None = None
    list_level: int | None = None
    qualified_only: bool = False
    has_default: bool = False
    has_technical_default: bool = False
    issue_size: float | None = None
    issue_date: date | None = None
    coupon_benchmark: str | None = None
    coupon_benchmark_spread: float | None = None
    days_to_redemption: int | None = None
    raw: dict[str, str] = field(default_factory=dict)


@dataclass
class CouponRow:
    coupon_date: date
    value: float | None
    value_rub: float | None
    facevalue: float | None


@dataclass
class AmortRow:
    amort_date: date
    value: float | None
    value_prc: float | None
    facevalue: float | None
    initial_facevalue: float | None
    data_source: str

    @property
    def is_maturity(self) -> bool:
        return self.data_source == "maturity"


@dataclass
class OfferRow:
    offer_date: date | None
    offer_type: str | None
    price: float | None


@dataclass
class Schedule:
    coupons: list[CouponRow] = field(default_factory=list)
    amortizations: list[AmortRow] = field(default_factory=list)
    offers: list[OfferRow] = field(default_factory=list)

    def amortization_only(self) -> list[AmortRow]:
        """Только настоящая амортизация — без строки погашения."""
        return [a for a in self.amortizations if not a.is_maturity]

    def maturity_row(self) -> AmortRow | None:
        for a in self.amortizations:
            if a.is_maturity:
                return a
        return None

    def future_offers(self, as_of: date) -> list[OfferRow]:
        return [o for o in self.offers if o.offer_date and o.offer_date > as_of]


def _desc_map(payload: dict) -> dict[str, str]:
    raw: dict[str, str] = {}
    for row in block(payload, "description"):
        name = row.get("name")
        if name:
            raw[str(name)] = row.get("value")
    return raw


def fetch_details(client: IssClient, secid: str, stamp: str | None = None) -> IssueDetails:
    payload = client.get(DESCRIPTION_PATH.format(secid=secid), stamp=stamp, **{"iss.only": "description"})
    raw = _desc_map(payload)

    def flag(name: str) -> bool:
        return str(raw.get(name, "0")).strip() in {"1", "true", "True"}

    return IssueDetails(
        secid=secid,
        isin=raw.get("ISIN"),
        name=raw.get("NAME"),
        shortname=raw.get("SHORTNAME"),
        issuer_name=raw.get("ISSUENAME"),
        emitter_id=raw.get("EMITTER_ID"),
        facevalue=num(raw.get("FACEVALUE")),
        initial_facevalue=num(raw.get("INITIALFACEVALUE")),
        faceunit=raw.get("FACEUNIT"),
        matdate=dt(raw.get("MATDATE")),
        offerdate=dt(raw.get("OFFERDATE")),
        coupon_frequency=int(num(raw.get("COUPONFREQUENCY")) or 0) or None,
        coupon_value=num(raw.get("COUPONVALUE")),
        coupon_percent=num(raw.get("COUPONPERCENT")),
        bond_type=raw.get("BOND_TYPE"),
        bond_subtype=raw.get("BOND_SUBTYPE"),
        list_level=int(num(raw.get("LISTLEVEL")) or 0) or None,
        qualified_only=flag("ISQUALIFIEDINVESTORS"),
        has_default=flag("HASDEFAULT"),
        has_technical_default=flag("HASTECHNICALDEFAULT"),
        issue_size=num(raw.get("ISSUESIZE")),
        issue_date=dt(raw.get("ISSUEDATE")),
        coupon_benchmark=raw.get("COUPON_BENCHMARK"),
        coupon_benchmark_spread=num(raw.get("COUPON_BENCHMARK_SPREAD")),
        days_to_redemption=int(num(raw.get("DAYSTOREDEMPTION")) or 0) or None,
        raw=raw,
    )


def _fetch_bondization_all(client: IssClient, secid: str, stamp: str | None) -> dict:
    """Загрузка bondization с постраничным добором.

    ISS отдаёт по 20 строк на блок и сообщает полное количество в курсоре.
    Без добора график обрывается на первой странице: у выпуска с
    ежемесячным купоном и сроком 3 года теряется большая часть выплат, и
    доходность считается неверно (вплоть до смешных 4% вместо 16%).
    """
    payload = client.get(BONDIZATION_PATH.format(secid=secid), stamp=stamp)
    merged = {k: v for k, v in payload.items() if not k.endswith(".cursor")}

    for name in ("coupons", "amortizations", "offers"):
        block_data = payload.get(name) or {}
        columns = block_data.get("columns") or []
        rows = list(block_data.get("data") or [])
        if not columns:
            continue

        cursor = payload.get(f"{name}.cursor") or {}
        total = 0
        if cursor.get("data"):
            idx = cursor["columns"].index("TOTAL") if "TOTAL" in cursor["columns"] else None
            if idx is not None and cursor["data"][0][idx] is not None:
                total = int(cursor["data"][0][idx])

        start = len(rows)
        while total and start < total:
            extra = client.get(
                BONDIZATION_PATH.format(secid=secid),
                stamp=stamp,
                start=start,
            )
            more = (extra.get(name) or {}).get("data") or []
            if not more:
                break
            rows.extend(more)
            start += len(more)

        merged[name] = {"columns": columns, "data": rows}

    return merged


def fetch_schedule(client: IssClient, secid: str, stamp: str | None = None) -> Schedule:
    payload = _fetch_bondization_all(client, secid, stamp)

    coupons = [
        CouponRow(
            coupon_date=dt(r.get("coupondate")),
            value=num(r.get("value")),
            value_rub=num(r.get("value_rub")),
            facevalue=num(r.get("facevalue")),
        )
        for r in block(payload, "coupons")
        if dt(r.get("coupondate"))
    ]

    amortizations = [
        AmortRow(
            amort_date=dt(r.get("amortdate")),
            value=num(r.get("value")),
            value_prc=num(r.get("valueprc")),
            facevalue=num(r.get("facevalue")),
            initial_facevalue=num(r.get("initialfacevalue")),
            data_source=str(r.get("data_source") or "unknown"),
        )
        for r in block(payload, "amortizations")
        if dt(r.get("amortdate"))
    ]

    offers = [
        OfferRow(
            offer_date=dt(r.get("offerdate")),
            offer_type=r.get("offertype"),
            price=num(r.get("price")),
        )
        for r in block(payload, "offers")
    ]

    return Schedule(
        coupons=sorted(coupons, key=lambda c: c.coupon_date),
        amortizations=sorted(amortizations, key=lambda a: a.amort_date),
        offers=offers,
    )


def _face_strictly_before(timeline: list[tuple[date, float]], target: date) -> float:
    """Номинал после амортизаций строго раньше target."""
    if not timeline:
        return 0.0
    initial = timeline[0][1]
    paid = 0.0
    for i, (pay_date, before) in enumerate(timeline):
        if pay_date >= target:
            break
        after = timeline[i + 1][1] if i + 1 < len(timeline) else 0.0
        paid += before - after
    return initial - paid


def amortization_face_timeline(schedule: Schedule, details: IssueDetails) -> list[tuple[date, float]]:
    """Восстановление остаточного номинала по графику амортизации.

    ISS не отдаёт абсолютный номинал на каждую дату, но даёт
    полный список амортизационных платежей в рублях и первоначальный номинал.
    Остаток восстанавливается вычитанием уже состоявшихся платежей.
    """
    initial = details.initial_facevalue or details.facevalue
    if not initial:
        return []
    rows = sorted(schedule.amortizations, key=lambda a: a.amort_date)
    if not rows:
        return []

    total = sum(a.value or 0.0 for a in rows)
    if abs(total - initial) > max(1.0, initial * 0.01):
        # График несогласован: восстанавливать по нему нельзя.
        return []

    timeline: list[tuple[date, float]] = []
    remaining = initial
    for a in rows:
        timeline.append((a.amort_date, remaining))
        remaining -= a.value or 0.0
    return timeline


def face_before(timeline: list[tuple[date, float]], target: date) -> float | None:
    """Номинал, на который начисляется купон в дату target.

    Это номинал ДО амортизации этой же даты. Проверено на RU000A106WZ2:
    купоны 59.84 / 44.88 / 29.92 / 14.96 = номинал 1000/750/500/250 * 24% * 91/365.
    """
    if not timeline:
        return None
    return _face_strictly_before(timeline, target)

def reconstruct_coupons(
    schedule: Schedule, details: IssueDetails, as_of: date
) -> tuple[list[CouponRow], str]:
    """Восстановление недостающих сумм купонов по объявленной ставке.

    У амортизируемых выпусков ISS часто не публикует будущие суммы, хотя
    ставка и график амортизации известны. Подставлять расчёт можно
    только после проверки на уже известных купонах: если формула не
    воспроизводит известные значения, восстановление отклоняется.

    Формула ISS: номинал ДО амортизации этой же даты
    * ставка * (фактические дни периода / 365).
    """
    rate = details.coupon_percent
    freq = details.coupon_frequency
    if not rate or rate <= 0:
        return [], "NO_RATE"

    timeline = amortization_face_timeline(schedule, details)
    if not timeline:
        return [], "NO_AMORTIZATION_TIMELINE"

    all_coupons = sorted(schedule.coupons, key=lambda c: c.coupon_date)
    future = [c for c in all_coupons if c.coupon_date > as_of]
    if not future:
        return [], "NO_FUTURE_COUPONS"

    reconstructed: list[CouponRow] = []
    for c in future:
        face = face_before(timeline, c.coupon_date)
        if face is None or face <= 0:
            return [], "FACE_UNKNOWN"

        idx = all_coupons.index(c)
        prev_date = all_coupons[idx - 1].coupon_date if idx > 0 else None
        days = (c.coupon_date - prev_date).days if prev_date else 0
        if days <= 0:
            days = 365.0 / (freq or 1)

        reconstructed.append(
            CouponRow(
                coupon_date=c.coupon_date,
                value=face * rate / 100.0 * (days / 365.0),
                value_rub=c.value_rub,
                facevalue=face,
            )
        )

    # Самопроверка на известных значениях
    checked = 0
    for original, calc in zip(future, reconstructed):
        if original.value is None:
            continue
        checked += 1
        tolerance = max(0.5, original.value * 0.02)
        if abs(original.value - calc.value) > tolerance:
            return [], "RECONSTRUCTION_MISMATCH"

    if checked == 0:
        return [], "NO_KNOWN_COUPON_TO_VERIFY"

    return reconstructed, "RECONSTRUCTED"


def coupon_is_fixed(schedule: Schedule, details: IssueDetails, as_of: date) -> tuple[bool, str]:
    """Проверка, что будущие купоны действительно фиксированы.

    Признак плавающего купона — ссылка на benchmark (ключевая ставка, RUONIA)
    или разные приведённые ставки по периодам. У амортизируемых выпусков
    суммы падают вместе с номиналом — это нормально, поэтому ставка
    нормируется на номинал на дату выплаты, а не на текущий facevalue.
    """
    if details.coupon_benchmark:
        return False, f"BENCHMARK:{details.coupon_benchmark}"

    future_all = [c for c in schedule.coupons if c.coupon_date > as_of]
    if not future_all:
        return False, "NO_FUTURE_COUPON_SCHEDULE"

    freq = details.coupon_frequency or 2
    timeline = amortization_face_timeline(schedule, details)

    rates: set[float] = set()
    unknown = 0
    for c in future_all:
        if c.value is None:
            unknown += 1
            continue
        face = face_before(timeline, c.coupon_date) if timeline else None
        base = face or c.facevalue or details.facevalue or 1000.0
        if base <= 0:
            return False, "BAD_FACEVALUE"
        rates.add(round(c.value / base * freq * 100, 4))

    if len(rates) > 1:
        return False, f"VARIABLE_COUPONS:{len(rates)}"

    if unknown == 0:
        return (True, "FIXED") if rates else (False, "NO_FUTURE_COUPON_SCHEDULE")

    # Часть сумм не опубликована. Восстановить их можно только
    # если ставка однозначна и расчёт подтверждается на известных выплатах.
    if not rates:
        return False, f"UNKNOWN_FUTURE_COUPON_VALUES:{unknown}"
    if not details.coupon_percent or not timeline:
        return False, f"UNKNOWN_FUTURE_COUPON_VALUES:{unknown}"

    _, status = reconstruct_coupons(schedule, details, as_of)
    if status != "RECONSTRUCTED":
        return False, f"UNRECONSTRUCTABLE:{status}"
    return True, "FIXED_RECONSTRUCTED"
