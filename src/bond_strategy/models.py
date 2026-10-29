"""Доменные модели аналитического ядра.

Только структуры и инварианты. Бизнес-логика — в analytics/validation/signals/portfolio.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Optional


class DataStatus(str, Enum):
    FRESH = "FRESH"
    STALE = "STALE"
    MISSING = "MISSING"
    CONFLICT = "CONFLICT"
    UNVERIFIED = "UNVERIFIED"

    @property
    def blocks_buy(self) -> bool:
        return self is not DataStatus.FRESH


_STATUS_SEVERITY = [
    DataStatus.CONFLICT,
    DataStatus.MISSING,
    DataStatus.UNVERIFIED,
    DataStatus.STALE,
    DataStatus.FRESH,
]


class Quality(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"


class SignalType(str, Enum):
    BUY = "BUY"
    WATCH = "WATCH"
    REVIEW = "REVIEW"
    SELL_REVIEW = "SELL_REVIEW"
    DATA_BLOCKED = "DATA_BLOCKED"


class OperationType(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    COUPON = "COUPON"
    AMORTIZATION = "AMORTIZATION"
    REDEMPTION = "REDEMPTION"


class RiskMode(str, Enum):
    NORMAL = "NORMAL"
    ENHANCED_CONTROL = "ENHANCED_CONTROL"
    CAPITAL_PROTECTION = "CAPITAL_PROTECTION"
    FULL_STOP = "FULL_STOP"


@dataclass(frozen=True)
class AmortizationPayment:
    """Погашение доли номинала на дату."""

    payment_date: date
    share_of_nominal: float

    def __post_init__(self) -> None:
        if not 0 < self.share_of_nominal <= 1:
            raise ValueError("share_of_nominal должен быть в (0, 1]")


@dataclass
class BondIssue:
    isin: str
    secid: str
    issuer_id: str
    issuer_name: str
    industry: str
    nominal: float = 1000.0
    initial_nominal: float = 1000.0          # до амортизации; база для долей
    coupon_type: str = "FIXED"
    coupon_rate: Optional[float] = None       # годовая ставка долей
    coupon_frequency: int = 2
    maturity_date: Optional[date] = None
    offer_date: Optional[date] = None
    amortization: list[AmortizationPayment] = field(default_factory=list)
    qualified_only: bool = False
    list_level: Optional[int] = None
    min_lot: int = 1
    quality: Quality = Quality.HIGH
    rating: Optional[str] = None
    rating_agency: Optional[str] = None
    rating_date: Optional[date] = None
    default_flag: bool = False
    restructuring_flag: bool = False
    security_name: Optional[str] = None      # SHORTNAME с борды, для отображения

    # рыночные данные
    clean_price: Optional[float] = None
    accrued_interest: Optional[float] = None
    price_date: Optional[date] = None
    avg_daily_turnover: Optional[float] = None
    turnover_days: int = 0  # сколько торговых дней учтено в среднем
    avg_spread: Optional[float] = None
    ofz_ytm: Optional[float] = None

    # фактический график купонов из ISS: [(дата выплаты, руб. на бумагу)]
    coupon_schedule: list[tuple[date, float]] = field(default_factory=list)

    status: dict[str, DataStatus] = field(default_factory=dict)

    def dirty_price(self) -> Optional[float]:
        if self.clean_price is None or self.accrued_interest is None:
            return None
        return self.clean_price + self.accrued_interest

    def critical_status(self) -> DataStatus:
        present = set(self.status.values())
        if not present:
            return DataStatus.MISSING
        for level in _STATUS_SEVERITY:
            if level in present:
                return level
        return DataStatus.FRESH

    def blocked_fields(self) -> list[str]:
        return [name for name, st in self.status.items() if st.blocks_buy]

    # Поля, без которых нельзя ни посчитать стоимость сделки, ни проверить лимит
    # по сроку, ни отнести бумагу к категории рейтинга. Набор отвечает на
    # требование раздела 3: «условия выпуска и данные для расчёта доступны».
    REQUIRED_FOR_CALC = (
        "clean_price",
        "accrued_interest",
        "maturity_date",
        "rating",
        "industry",
    )

    def missing_fields(self) -> list[str]:
        """Обязательные для расчёта поля, которые пусты.

        Отсутствие поля не заменяется значением по умолчанию: оценённая
        «примерно» доля портфеля хуже, чем честный отказ.
        """
        return [name for name in self.REQUIRED_FOR_CALC if getattr(self, name, None) is None]

    def data_completeness(self) -> float:
        """Доля заполненных обязательных полей, от 0 до 1.

        Считается по значениям, а не по `status`: статус отвечает на вопрос
        «свежие ли данные», а здесь нужен ответ «есть ли они вообще». Выпуск со
        свежей, но пустой ценой должен отсеиваться.
        """
        total = len(self.REQUIRED_FOR_CALC)
        if not total:
            return 1.0
        return (total - len(self.missing_fields())) / total


@dataclass
class Position:
    isin: str
    quantity: int
    nominal_outstanding: float
    purchase_cost: float
    clean_price: float
    accrued_interest: float
    received_coupons: float = 0.0
    received_amortization: float = 0.0

    def market_value(self, clean: float, nkd: float) -> float:
        return self.quantity * (clean + nkd) / 100.0 * self.nominal_outstanding


@dataclass(frozen=True)
class Trade:
    event_key: str
    isin: str
    operation_type: OperationType
    event_date: date
    quantity: int
    clean_price: float
    accrued_interest: float
    nominal: float
    commission: float
    amount: float
    signal_id: Optional[str] = None


@dataclass
class Signal:
    signal_id: str
    isin: str
    signal_type: SignalType
    created_at: date
    score: float
    reasons: list[str] = field(default_factory=list)
    failed_conditions: list[str] = field(default_factory=list)
    suggested_amount: Optional[float] = None
    suggested_quantity: Optional[int] = None
    config_id: str = ""
