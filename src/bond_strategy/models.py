"""Доменные модели аналитического ядра."""
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
    COMMISSION = "COMMISSION"


class RiskMode(str, Enum):
    NORMAL = "NORMAL"
    ENHANCED_CONTROL = "ENHANCED_CONTROL"
    CAPITAL_PROTECTION = "CAPITAL_PROTECTION"
    FULL_STOP = "FULL_STOP"

class AmortizationPayment:
    """Одна амортизационная выплата: доля номинала на дату."""

    __slots__ = ("payment_date", "share_of_nominal")

    def __init__(self, payment_date: date, share_of_nominal: float) -> None:
        if not 0 < share_of_nominal <= 1:
            raise ValueError("share_of_nominal должен быть в (0, 1]")
        self.payment_date = payment_date
        self.share_of_nominal = share_of_nominal

    def __repr__(self) -> str:
        return f"Amortization({self.payment_date}, {self.share_of_nominal:.2%})"


class CouponPayment:
    """Фактическая купонная выплата на одну бумагу в рублях.

    Нужна потому, что у реальных выпусков ставка меняется от периода к периоду,
    а купон начисляется на остаточный номинал. Генерация потока из одной
    постоянной ставки дала бы неверную доходность.
    """

    __slots__ = ("payment_date", "amount_per_bond")

    def __init__(self, payment_date: date, amount_per_bond: float) -> None:
        if amount_per_bond < 0:
            raise ValueError("amount_per_bond не может быть отрицательным")
        self.payment_date = payment_date
        self.amount_per_bond = amount_per_bond

    def __repr__(self) -> str:
        return f"Coupon({self.payment_date}, {self.amount_per_bond:.2f})"


@dataclass
class BondIssue:
    isin: str
    ticker: str
    issuer_id: str
    issuer_name: str
    industry: str
    nominal: float = 1000.0
    coupon_type: str = "FIXED"
    coupon_rate: Optional[float] = None
    coupon_frequency: int = 2
    maturity_date: Optional[date] = None
    offer_flag: bool = False
    amortization: list[AmortizationPayment] = field(default_factory=list)
    coupons: list[CouponPayment] = field(default_factory=list)
    qualified_only: bool = False
    min_lot: int = 1
    quality: Quality = Quality.HIGH
    rating: Optional[str] = None
    rating_agency: Optional[str] = None
    rating_date: Optional[date] = None
    default_flag: bool = False
    restructuring_flag: bool = False
    reporting_sufficient: bool = True

    net_debt_to_ebitda: Optional[float] = None
    interest_coverage: Optional[float] = None
    list_level: Optional[int] = None
    credit_basis: str = "UNKNOWN"

    clean_price: Optional[float] = None
    accrued_interest: Optional[float] = None
    price_date: Optional[date] = None
    avg_daily_turnover: Optional[float] = None
    avg_spread: Optional[float] = None
    ytm: Optional[float] = None
    ofz_ytm: Optional[float] = None

    status: dict[str, DataStatus] = field(default_factory=dict)

    def dirty_price(self) -> Optional[float]:
        if self.clean_price is None or self.accrued_interest is None:
            return None
        return self.clean_price + self.accrued_interest

    def critical_status(self) -> DataStatus:
        if not self.status:
            return DataStatus.MISSING
        order = [
            DataStatus.CONFLICT,
            DataStatus.MISSING,
            DataStatus.UNVERIFIED,
            DataStatus.STALE,
            DataStatus.FRESH,
        ]
        present = set(self.status.values())
        for level in order:
            if level in present:
                return level
        return DataStatus.FRESH

    def blocked_fields(self) -> list[str]:
        return [name for name, st in self.status.items() if st.blocks_buy]


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
    status: str = "OPEN"

    def market_value(self, clean: float, nkd: float) -> float:
        return self.quantity * (clean + nkd) / 100.0 * self.nominal_outstanding


@dataclass
class Trade:
    trade_id: str
    isin: str
    operation_type: OperationType
    event_date: date
    quantity: int
    clean_price: float
    accrued_interest: float
    nominal: float
    commission: float
    amount: float
    event_key: str
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
    review_date: Optional[date] = None
    suggested_amount: Optional[float] = None
    config_id: str = ""
