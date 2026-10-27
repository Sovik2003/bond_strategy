"""Модели портфеля: денежные средства, позиции, операции, лимиты.

Все события идемпотентны по event_key: повторная загрузка купона или
амортизации не начисляет выплату второй раз.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..config import StrategyConfig
from ..models import (
    BondIssue,
    OperationType,
    Position,
    Quality,
    RiskMode,
    Trade,
)


class LimitViolation(Exception):
    def __init__(self, code: str, text: str) -> None:
        super().__init__(text)
        self.code = code
        self.text = text


Prices = dict[str, tuple[float, float]]  # isin -> (clean, nkd)


@dataclass
class Portfolio:
    cfg: StrategyConfig
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    trades: list[Trade] = field(default_factory=list)
    processed_keys: set[str] = field(default_factory=set)
    peak_value: float = 0.0
    quality_by_isin: dict[str, Quality] = field(default_factory=dict)
    issuer_by_isin: dict[str, str] = field(default_factory=dict)
    industry_by_isin: dict[str, str] = field(default_factory=dict)

    # ---------- оценка ----------

    def _position_value(self, pos: Position, prices: Prices) -> float:
        if pos.isin in prices:
            clean, nkd = prices[pos.isin]
            return pos.market_value(clean, nkd)
        return pos.purchase_cost

    def bonds_value(self, prices: Prices) -> float:
        return sum(self._position_value(p, prices) for p in self.positions.values())

    def total_value(self, prices: Prices) -> float:
        return self.cash + self.bonds_value(prices)

    def _share(self, predicate, prices: Prices) -> float:
        total = self.total_value(prices)
        if total <= 0:
            return 0.0
        value = sum(
            self._position_value(p, prices)
            for isin, p in self.positions.items()
            if predicate(isin)
        )
        return value / total

    def issuer_share(self, issuer_id: str, prices: Prices) -> float:
        return self._share(lambda i: self.issuer_by_isin.get(i) == issuer_id, prices)

    def issue_share(self, isin: str, prices: Prices) -> float:
        return self._share(lambda i: i == isin, prices)

    def industry_share(self, industry: str, prices: Prices) -> float:
        return self._share(lambda i: self.industry_by_isin.get(i) == industry, prices)

    def medium_quality_share(self, prices: Prices) -> float:
        return self._share(lambda i: self.quality_by_isin.get(i) is Quality.MEDIUM, prices)

    def drawdown(self, prices: Prices) -> float:
        current = self.total_value(prices)
        if self.peak_value <= 0:
            return 0.0
        return (current - self.peak_value) / self.peak_value

    def update_peak(self, prices: Prices) -> None:
        self.peak_value = max(self.peak_value, self.total_value(prices))

    def risk_mode(self, prices: Prices) -> RiskMode:
        dd = self.drawdown(prices)
        if dd <= -0.12:
            return RiskMode.CAPITAL_PROTECTION
        if dd <= -0.08:
            return RiskMode.ENHANCED_CONTROL
        return RiskMode.NORMAL

    # ---------- операции ----------

    def buy(
        self,
        issue: BondIssue,
        quantity: int,
        trade_date: date,
        commission_rate: float = 0.0,
        signal_id: str | None = None,
    ) -> Trade:
        if issue.clean_price is None or issue.accrued_interest is None:
            raise LimitViolation("NO_PRICE", "Нет цены или НКД")
        if quantity <= 0:
            raise LimitViolation("BAD_QUANTITY", "Количество должно быть положительным")

        gross = quantity * (issue.clean_price + issue.accrued_interest) / 100.0 * issue.nominal
        commission = gross * commission_rate
        total = gross + commission

        if total > self.cash + 1e-9:
            raise LimitViolation(
                "NO_CASH", f"Нужно {total:,.2f} ₽, доступно {self.cash:,.2f} ₽"
            )

        key = f"{issue.isin}-BUY-{trade_date.isoformat()}"
        if key in self.processed_keys:
            raise LimitViolation("DUPLICATE", "Операция уже учтена")

        self.cash -= total
        existing = self.positions.get(issue.isin)
        if existing:
            new_qty = existing.quantity + quantity
            existing.clean_price = (
                existing.clean_price * existing.quantity + issue.clean_price * quantity
            ) / new_qty
            existing.quantity = new_qty
            existing.purchase_cost += total
        else:
            self.positions[issue.isin] = Position(
                isin=issue.isin,
                quantity=quantity,
                nominal_outstanding=issue.nominal,
                purchase_cost=total,
                clean_price=issue.clean_price,
                accrued_interest=issue.accrued_interest,
            )

        self.quality_by_isin[issue.isin] = issue.quality
        self.issuer_by_isin[issue.isin] = issue.issuer_id
        self.industry_by_isin[issue.isin] = issue.industry

        trade = Trade(
            event_key=key,
            isin=issue.isin,
            operation_type=OperationType.BUY,
            event_date=trade_date,
            quantity=quantity,
            clean_price=issue.clean_price,
            accrued_interest=issue.accrued_interest,
            nominal=issue.nominal,
            commission=commission,
            amount=total,
            signal_id=signal_id,
        )
        self.trades.append(trade)
        self.processed_keys.add(key)
        return trade

    def sell(
        self,
        issue: BondIssue,
        quantity: int,
        trade_date: date,
        commission_rate: float = 0.0,
    ) -> Trade:
        pos = self.positions.get(issue.isin)
        if pos is None:
            raise LimitViolation("NO_POSITION", "Позиции нет")
        if not 0 < quantity <= pos.quantity:
            raise LimitViolation("NOT_ENOUGH", "Недостаточно бумаг")
        if issue.clean_price is None or issue.accrued_interest is None:
            raise LimitViolation("NO_PRICE", "Нет цены или НКД")

        key = f"{issue.isin}-SELL-{trade_date.isoformat()}-{quantity}"
        if key in self.processed_keys:
            raise LimitViolation("DUPLICATE", "Операция уже учтена")

        gross = (
            quantity
            * (issue.clean_price + issue.accrued_interest)
            / 100.0
            * pos.nominal_outstanding
        )
        commission = gross * commission_rate
        proceeds = gross - commission

        share_sold = quantity / pos.quantity
        self.cash += proceeds
        pos.purchase_cost -= pos.purchase_cost * share_sold
        pos.quantity -= quantity
        if pos.quantity == 0:
            del self.positions[issue.isin]

        trade = Trade(
            event_key=key,
            isin=issue.isin,
            operation_type=OperationType.SELL,
            event_date=trade_date,
            quantity=quantity,
            clean_price=issue.clean_price,
            accrued_interest=issue.accrued_interest,
            nominal=pos.nominal_outstanding,
            commission=commission,
            amount=proceeds,
        )
        self.trades.append(trade)
        self.processed_keys.add(key)
        return trade

    def process_coupon(self, isin: str, payment_date: date, amount_per_bond: float) -> float:
        key = f"{isin}-COUPON-{payment_date.isoformat()}"
        if key in self.processed_keys:
            return 0.0
        self.processed_keys.add(key)
        pos = self.positions.get(isin)
        if pos is None:
            return 0.0

        payment = amount_per_bond * pos.quantity
        self.cash += payment
        pos.received_coupons += payment
        self.trades.append(
            Trade(
                event_key=key,
                isin=isin,
                operation_type=OperationType.COUPON,
                event_date=payment_date,
                quantity=pos.quantity,
                clean_price=0.0,
                accrued_interest=0.0,
                nominal=pos.nominal_outstanding,
                commission=0.0,
                amount=payment,
            )
        )
        return payment

    def process_amortization(
        self, isin: str, payment_date: date, amount_per_bond: float
    ) -> float:
        key = f"{isin}-AMORT-{payment_date.isoformat()}"
        if key in self.processed_keys:
            return 0.0
        self.processed_keys.add(key)
        pos = self.positions.get(isin)
        if pos is None:
            return 0.0

        payment = amount_per_bond * pos.quantity
        nominal_before = pos.nominal_outstanding
        self.cash += payment
        pos.received_amortization += payment
        pos.nominal_outstanding = max(0.0, pos.nominal_outstanding - amount_per_bond)

        self.trades.append(
            Trade(
                event_key=key,
                isin=isin,
                operation_type=OperationType.AMORTIZATION,
                event_date=payment_date,
                quantity=pos.quantity,
                clean_price=0.0,
                accrued_interest=0.0,
                nominal=nominal_before,
                commission=0.0,
                amount=payment,
            )
        )
        if pos.nominal_outstanding <= 1e-9:
            del self.positions[isin]
        return payment

    def process_redemption(self, isin: str, payment_date: date) -> float:
        key = f"{isin}-REDEEM-{payment_date.isoformat()}"
        if key in self.processed_keys:
            return 0.0
        self.processed_keys.add(key)
        pos = self.positions.get(isin)
        if pos is None:
            return 0.0

        payment = pos.nominal_outstanding * pos.quantity
        self.cash += payment
        self.trades.append(
            Trade(
                event_key=key,
                isin=isin,
                operation_type=OperationType.REDEMPTION,
                event_date=payment_date,
                quantity=pos.quantity,
                clean_price=0.0,
                accrued_interest=0.0,
                nominal=pos.nominal_outstanding,
                commission=0.0,
                amount=payment,
            )
        )
        del self.positions[isin]
        return payment


def check_limits(
    portfolio: Portfolio,
    issue: BondIssue,
    amount: float,
    prices: Prices,
    cfg: StrategyConfig,
) -> None:
    """Проверка лимитов перед виртуальной покупкой."""
    total = portfolio.total_value(prices)
    if total <= 0:
        raise LimitViolation("EMPTY_PORTFOLIO", "Стоимость портфеля не определена")

    issuer_max = float(cfg.get("limits", "issuer_max_share"))
    issue_max = float(cfg.get("limits", "issue_max_share"))
    industry_max = float(cfg.get("limits", "industry_max_share"))
    medium_max = float(cfg.get("limits", "medium_quality_max_share"))
    reserve = cfg.reserve_base

    current_issuer = portfolio.issuer_share(issue.issuer_id, prices) * total
    if (current_issuer + amount) / total > issuer_max + 1e-9:
        raise LimitViolation("ISSUER_LIMIT", f"Эмитент превысит {issuer_max:.0%} капитала")

    current_issue = portfolio.issue_share(issue.isin, prices) * total
    if (current_issue + amount) / total > issue_max + 1e-9:
        raise LimitViolation("ISSUE_LIMIT", f"Выпуск превысит {issue_max:.0%} капитала")

    current_industry = portfolio.industry_share(issue.industry, prices) * total
    if (current_industry + amount) / total > industry_max + 1e-9:
        raise LimitViolation("INDUSTRY_LIMIT", f"Отрасль превысит {industry_max:.0%} капитала")

    if issue.quality is Quality.MEDIUM:
        current_medium = portfolio.medium_quality_share(prices) * total
        if (current_medium + amount) / total > medium_max + 1e-9:
            raise LimitViolation("MEDIUM_LIMIT", f"Среднее качество превысит {medium_max:.0%}")

    if portfolio.cash - amount < reserve - 1e-9:
        raise LimitViolation("RESERVE_LIMIT", f"Резерв опустится ниже {reserve:,.0f} ₽")


def position_size(
    portfolio: Portfolio,
    issue: BondIssue,
    prices: Prices,
    cfg: StrategyConfig,
    commission_rate: float = 0.0,
) -> int:
    """Максимальное число бумаг, проходящее все лимиты. 0 — если не проходит."""
    if issue.clean_price is None or issue.accrued_interest is None:
        return 0

    lot_cost = (
        issue.min_lot * (issue.clean_price + issue.accrued_interest) / 100.0 * issue.nominal
    ) * (1.0 + commission_rate)
    if lot_cost <= 0:
        return 0

    total = portfolio.total_value(prices)
    issuer_max = float(cfg.get("limits", "issuer_max_share"))
    issue_max = float(cfg.get("limits", "issue_max_share"))
    industry_max = float(cfg.get("limits", "industry_max_share"))
    medium_max = float(cfg.get("limits", "medium_quality_max_share"))

    caps = [
        portfolio.cash - cfg.reserve_base,
        total * issuer_max - portfolio.issuer_share(issue.issuer_id, prices) * total,
        total * issue_max - portfolio.issue_share(issue.isin, prices) * total,
        total * industry_max - portfolio.industry_share(issue.industry, prices) * total,
    ]
    if issue.quality is Quality.MEDIUM:
        caps.append(
            total * medium_max - portfolio.medium_quality_share(prices) * total
        )

    budget = min(caps)
    if budget <= 0:
        return 0
    lots = int(budget // lot_cost)
    return lots * issue.min_lot
