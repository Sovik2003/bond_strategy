"""Модельный портфель: денежные средства, позиции, лимиты, события."""
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

    def bonds_value(self, prices: dict[str, tuple[float, float]]) -> float:
        total = 0.0
        for isin, pos in self.positions.items():
            if isin in prices:
                clean, nkd = prices[isin]
                total += pos.market_value(clean, nkd)
            else:
                total += pos.purchase_cost
        return total

    def total_value(self, prices: dict[str, tuple[float, float]]) -> float:
        return self.cash + self.bonds_value(prices)

    def drawdown(self, prices: dict[str, tuple[float, float]]) -> float:
        if self.peak_value <= 0:
            return 0.0
        return (self.total_value(prices) - self.peak_value) / self.peak_value

    def update_peak(self, prices: dict[str, tuple[float, float]]) -> None:
        self.peak_value = max(self.peak_value, self.total_value(prices))

    def _group_value(self, key_map: dict[str, str], key: str, prices) -> float:
        value = 0.0
        for isin, pos in self.positions.items():
            if key_map.get(isin) == key:
                if isin in prices:
                    clean, nkd = prices[isin]
                    value += pos.market_value(clean, nkd)
                else:
                    value += pos.purchase_cost
        return value

    def issuer_share(self, issuer_id: str, prices) -> float:
        total = self.total_value(prices)
        return 0.0 if total <= 0 else self._group_value(self.issuer_by_isin, issuer_id, prices) / total

    def industry_share(self, industry: str, prices) -> float:
        total = self.total_value(prices)
        return 0.0 if total <= 0 else self._group_value(self.industry_by_isin, industry, prices) / total

    def medium_quality_share(self, prices) -> float:
        total = self.total_value(prices)
        if total <= 0:
            return 0.0
        value = 0.0
        for isin, pos in self.positions.items():
            if self.quality_by_isin.get(isin) is Quality.MEDIUM:
                if isin in prices:
                    clean, nkd = prices[isin]
                    value += pos.market_value(clean, nkd)
                else:
                    value += pos.purchase_cost
        return value / total

    def risk_mode(self, prices) -> RiskMode:
        dd = self.drawdown(prices)
        if dd <= -0.12:
            return RiskMode.CAPITAL_PROTECTION
        if dd <= -0.08 or self.cash < 8000:
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

        gross = quantity * (issue.clean_price + issue.accrued_interest) / 100.0 * issue.nominal
        commission = gross * commission_rate
        total = gross + commission

        if total > self.cash:
            raise LimitViolation("NO_CASH", f"Нужно {total:,.2f} ₽, доступно {self.cash:,.2f} ₽")

        event_key = f"{issue.isin}-BUY-{trade_date.isoformat()}"
        if event_key in self.processed_keys:
            raise LimitViolation("DUPLICATE", "Операция уже учтена")

        self.cash -= total
        existing = self.positions.get(issue.isin)
        if existing:
            prev_qty = existing.quantity
            existing.quantity += quantity
            existing.purchase_cost += total
            existing.clean_price = (
                existing.clean_price * prev_qty + issue.clean_price * quantity
            ) / existing.quantity
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
            trade_id=event_key,
            isin=issue.isin,
            operation_type=OperationType.BUY,
            event_date=trade_date,
            quantity=quantity,
            clean_price=issue.clean_price,
            accrued_interest=issue.accrued_interest,
            nominal=issue.nominal,
            commission=commission,
            amount=total,
            event_key=event_key,
            signal_id=signal_id,
        )
        self.trades.append(trade)
        self.processed_keys.add(event_key)
        return trade

    def sell(self, issue: BondIssue, quantity: int, trade_date: date, commission_rate: float = 0.0) -> Trade:
        pos = self.positions.get(issue.isin)
        if pos is None:
            raise LimitViolation("NO_POSITION", "Позиции нет")
        if quantity > pos.quantity:
            raise LimitViolation("NOT_ENOUGH", "Недостаточно бумаг")
        if issue.clean_price is None or issue.accrued_interest is None:
            raise LimitViolation("NO_PRICE", "Нет цены или НКД")

        event_key = f"{issue.isin}-SELL-{trade_date.isoformat()}-{quantity}"
        if event_key in self.processed_keys:
            raise LimitViolation("DUPLICATE", "Операция уже учтена")

        gross = quantity * (issue.clean_price + issue.accrued_interest) / 100.0 * pos.nominal_outstanding
        commission = gross * commission_rate
        proceeds = gross - commission

        share_sold = quantity / pos.quantity
        self.cash += proceeds
        pos.purchase_cost -= pos.purchase_cost * share_sold
        pos.quantity -= quantity
        if pos.quantity == 0:
            pos.status = "CLOSED"
            del self.positions[issue.isin]

        trade = Trade(
            trade_id=event_key,
            isin=issue.isin,
            operation_type=OperationType.SELL,
            event_date=trade_date,
            quantity=quantity,
            clean_price=issue.clean_price,
            accrued_interest=issue.accrued_interest,
            nominal=pos.nominal_outstanding,
            commission=commission,
            amount=proceeds,
            event_key=event_key,
        )
        self.trades.append(trade)
        self.processed_keys.add(event_key)
        return trade

    def process_coupon(self, isin: str, payment_date: date, amount_per_bond: float) -> float:
        event_key = f"{isin}-COUPON-{payment_date.isoformat()}"
        if event_key in self.processed_keys:
            return 0.0
        pos = self.positions.get(isin)
        self.processed_keys.add(event_key)
        if pos is None:
            return 0.0

        payment = amount_per_bond * pos.quantity
        self.cash += payment
        pos.received_coupons += payment
        self.trades.append(
            Trade(event_key, isin, OperationType.COUPON, payment_date, pos.quantity,
                  0.0, 0.0, pos.nominal_outstanding, 0.0, payment, event_key)
        )
        return payment

    def process_amortization(
        self, isin: str, payment_date: date, amount_per_bond: float, nominal_before: float
    ) -> float:
        event_key = f"{isin}-AMORT-{payment_date.isoformat()}"
        if event_key in self.processed_keys:
            return 0.0
        pos = self.positions.get(isin)
        self.processed_keys.add(event_key)
        if pos is None:
            return 0.0

        payment = amount_per_bond * pos.quantity
        self.cash += payment
        pos.received_amortization += payment
        pos.nominal_outstanding = max(0.0, pos.nominal_outstanding - amount_per_bond)
        self.trades.append(
            Trade(event_key, isin, OperationType.AMORTIZATION, payment_date, pos.quantity,
                  0.0, 0.0, nominal_before, 0.0, payment, event_key)
        )
        if pos.nominal_outstanding <= 1e-9:
            pos.status = "CLOSED"
            del self.positions[isin]
        return payment

    def process_redemption(self, isin: str, payment_date: date) -> float:
        event_key = f"{isin}-REDEEM-{payment_date.isoformat()}"
        if event_key in self.processed_keys:
            return 0.0
        pos = self.positions.get(isin)
        self.processed_keys.add(event_key)
        if pos is None:
            return 0.0

        payment = pos.nominal_outstanding * pos.quantity
        self.cash += payment
        self.trades.append(
            Trade(event_key, isin, OperationType.REDEMPTION, payment_date, pos.quantity,
                  0.0, 0.0, pos.nominal_outstanding, 0.0, payment, event_key)
        )
        pos.status = "CLOSED"
        del self.positions[isin]
        return payment


def check_limits(
    portfolio: Portfolio, issue: BondIssue, amount: float, prices, cfg: StrategyConfig
) -> None:
    total = portfolio.total_value(prices)
    if total <= 0:
        raise LimitViolation("EMPTY_PORTFOLIO", "Стоимость портфеля не определена")

    issuer_max = float(cfg.get("limits", "issuer_max_share"))
    issue_max = float(cfg.get("limits", "issue_max_share"))
    industry_max = float(cfg.get("limits", "industry_max_share"))
    medium_max = float(cfg.get("limits", "medium_quality_max_share"))
    reserve = cfg.reserve_base

    if (portfolio.issuer_share(issue.issuer_id, prices) * total + amount) / total > issuer_max + 1e-9:
        raise LimitViolation("ISSUER_LIMIT", f"Эмитент превысит {issuer_max:.0%} капитала")

    pos = portfolio.positions.get(issue.isin)
    current_issue = 0.0
    if pos is not None:
        if issue.isin in prices:
            clean, nkd = prices[issue.isin]
            current_issue = pos.market_value(clean, nkd)
        else:
            current_issue = pos.purchase_cost
    if (current_issue + amount) / total > issue_max + 1e-9:
        raise LimitViolation("ISSUE_LIMIT", f"Выпуск превысит {issue_max:.0%} капитала")

    if (portfolio.industry_share(issue.industry, prices) * total + amount) / total > industry_max + 1e-9:
        raise LimitViolation("INDUSTRY_LIMIT", f"Отрасль превысит {industry_max:.0%} капитала")

    if issue.quality is Quality.MEDIUM:
        if (portfolio.medium_quality_share(prices) * total + amount) / total > medium_max + 1e-9:
            raise LimitViolation("MEDIUM_LIMIT", f"Среднее качество превысит {medium_max:.0%}")

    if portfolio.cash - amount < reserve - 1e-9:
        raise LimitViolation("RESERVE_LIMIT", f"Резерв опустится ниже {reserve:,.0f} ₽")


def position_size(
    portfolio: Portfolio, issue: BondIssue, prices, cfg: StrategyConfig, commission_rate: float = 0.0
) -> int:
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

    existing = portfolio.positions.get(issue.isin)
    if existing is not None and issue.isin in prices:
        clean, nkd = prices[issue.isin]
        issue_value = existing.market_value(clean, nkd)
    elif existing is not None:
        issue_value = existing.purchase_cost
    else:
        issue_value = 0.0

    caps = [
        portfolio.cash - cfg.reserve_base,
        total * issuer_max - portfolio.issuer_share(issue.issuer_id, prices) * total,
        total * issue_max - issue_value,
        total * industry_max - portfolio.industry_share(issue.industry, prices) * total,
    ]
    if issue.quality is Quality.MEDIUM:
        caps.append(total * medium_max - portfolio.medium_quality_share(prices) * total)

    budget = min(caps)
    if budget <= 0:
        return 0
    return int(budget // lot_cost) * issue.min_lot
