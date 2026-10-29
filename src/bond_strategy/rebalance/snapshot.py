"""Снимок портфеля: состояние, доли и проекция «после предполагаемой сделки».

Почему неизменяемое состояние. Планировщик перебирает варианты покупок и после
каждого шага должен видеть портфель заново. Если бы он правил один общий
объект, откат после неудачной попытки стал бы отдельной задачей с отдельными
ошибками. Здесь `with_purchase` возвращает новый снимок, а старый остаётся
нетронутым — сравнивать «до» и «после» можно без копирования вручную.

Единица количества. В `Holding.quantity` лежит число облигаций, а не лотов.
Лот — свойство рынка (`min_lot`), и в разных выпусках он разный: 1, 10, а
иногда и больше. Смешивать их в одном поле — верный способ купить в десять раз
больше задуманного, поэтому перевод лотов в бумаги происходит только в
`with_purchase`, а дальше везде считается в бумагах.

Стоимость портфеля — рыночная стоимость бумаг с НКД плюс денежный остаток.
Именно эта величина стоит в знаменателе всех долей из раздела 2.1, поэтому она
считается в одном месте и не пересчитывается каждым лимитом по-своему.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date

from ..analytics.cashflows import CashFlow, build_schedule
from ..analytics.duration import duration_coverage, macaulay_duration, portfolio_duration
from ..analytics.yield_calc import calculate_ytm
from ..models import BondIssue
from .config import LadderBand

DAYS_IN_YEAR = 365.0


@dataclass(frozen=True)
class Holding:
    """Позиция портфеля: выпуск и количество бумаг (не лотов)."""

    issue: BondIssue
    quantity: int

    @property
    def value(self) -> float:
        """Рыночная стоимость позиции с НКД.

        Без цены или НКД стоимость неизвестна, и подставлять номинал нельзя:
        доля такой позиции в портфеле была бы выдумана. Ноль здесь честнее —
        позиция попадёт в отчёт как неоценённая.
        """
        dirty = self.issue.dirty_price()
        if dirty is None:
            return 0.0
        return self.quantity * dirty / 100.0 * self.issue.nominal


@dataclass(frozen=True)
class Exposure:
    """Доли, по которым проверяются лимиты раздела 2.1."""

    issue: dict[str, float] = field(default_factory=dict)
    issuer: dict[str, float] = field(default_factory=dict)
    group: dict[str, float] = field(default_factory=dict)
    industry: dict[str, float] = field(default_factory=dict)
    band: dict[str, float] = field(default_factory=dict)

    low_rating: float = 0.0
    over_horizon: float = 0.0


class PortfolioState:
    """Неизменяемый снимок портфеля.

    Все производные величины (доли, дюрация, купоны) считаются от одного и того
    же знаменателя — `total_value`. Если бы каждый лимит делил на своё, доли
    перестали бы складываться в 100%.
    """

    def __init__(
        self,
        as_of: date,
        cash: float,
        holdings: list[Holding],
        *,
        issuer_groups: dict[str, str] | None = None,
        band_of=None,
        horizon_years: float = 5.0,
        low_rating_levels: frozenset[str] | None = None,
    ) -> None:
        self.as_of = as_of
        self.cash = float(cash)
        self._holdings = tuple(h for h in holdings if h.quantity > 0)
        self._issuer_groups = issuer_groups or {}
        self._band_of = band_of
        self.horizon_years = horizon_years
        self._low_rating_levels = low_rating_levels or frozenset()
        self._value_cache: float | None = None

    # --- состав ---------------------------------------------------------

    @property
    def holdings(self) -> tuple[Holding, ...]:
        return self._holdings

    def by_isin(self) -> dict[str, Holding]:
        return {h.issue.isin: h for h in self._holdings}

    def quantity(self, isin: str) -> int:
        holding = self.by_isin().get(isin)
        return holding.quantity if holding else 0

    def group_of(self, issue: BondIssue) -> str:
        return self._issuer_groups.get(issue.issuer_id, issue.issuer_id)

    # --- стоимость ------------------------------------------------------

    @property
    def bonds_value(self) -> float:
        return sum(h.value for h in self._holdings)

    @property
    def total_value(self) -> float:
        """Стоимость портфеля = бумаги с НКД + денежный остаток.

        Это знаменатель всех долей. При пустом портфеле и нулевом остатке
        знаменатель нулевой — тогда доли равны нулю, а не делятся на ноль.
        """
        if self._value_cache is None:
            self._value_cache = self.bonds_value + self.cash
        return self._value_cache

    def share(self, amount: float) -> float:
        total = self.total_value
        if total <= 0:
            return 0.0
        return amount / total

    # --- сроки и рейтинг ------------------------------------------------

    def years_to_maturity(self, issue: BondIssue) -> float | None:
        if issue.maturity_date is None:
            return None
        return max((issue.maturity_date - self.as_of).days, 0) / DAYS_IN_YEAR

    def band_of(self, issue: BondIssue) -> LadderBand | None:
        if self._band_of is None:
            return None
        return self._band_of(self.years_to_maturity(issue))

    def is_low_rating(self, issue: BondIssue) -> bool:
        return bool(issue.rating) and issue.rating in self._low_rating_levels

    def is_over_horizon(self, issue: BondIssue) -> bool:
        years = self.years_to_maturity(issue)
        return years is not None and years > self.horizon_years

    # --- доли -----------------------------------------------------------

    def exposure(self) -> Exposure:
        """Доли по всем разрезам сразу, от одного знаменателя."""
        total = self.total_value
        issue: dict[str, float] = {}
        issuer: dict[str, float] = {}
        group: dict[str, float] = {}
        industry: dict[str, float] = {}
        band: dict[str, float] = {}
        low_rating = 0.0
        over_horizon = 0.0

        for holding in self._holdings:
            value = holding.value
            share = value / total if total > 0 else 0.0
            item = holding.issue

            issue[item.isin] = issue.get(item.isin, 0.0) + share
            issuer[item.issuer_id] = issuer.get(item.issuer_id, 0.0) + share
            gid = self.group_of(item)
            group[gid] = group.get(gid, 0.0) + share
            industry[item.industry] = industry.get(item.industry, 0.0) + share

            ladder = self.band_of(item)
            if ladder is not None:
                band[ladder.code] = band.get(ladder.code, 0.0) + share

            if self.is_low_rating(item):
                low_rating += share
            if self.is_over_horizon(item):
                over_horizon += share

        return Exposure(
            issue=issue,
            issuer=issuer,
            group=group,
            industry=industry,
            band=band,
            low_rating=low_rating,
            over_horizon=over_horizon,
        )

    # --- денежный поток и дюрация ---------------------------------------

    def coupon_profile(self, flows: dict[str, list[CashFlow]] | None = None) -> dict[str, float]:
        """Ожидаемые купоны, амортизация и погашения по месяцам (YYYY-MM)."""
        out: dict[str, float] = {}
        for holding in self._holdings:
            schedule = (flows or {}).get(holding.issue.isin)
            if schedule is None:
                try:
                    schedule = build_schedule(holding.issue, self.as_of)
                except ValueError:
                    continue
            for flow in schedule:
                key = flow.payment_date.strftime("%Y-%m")
                out[key] = out.get(key, 0.0) + flow.amount_per_bond * holding.quantity
        return dict(sorted(out.items()))

    def duration(self) -> tuple[float | None, float]:
        """(дюрация портфеля, покрытие).

        Взвешивание идёт по доле в стоимости портфеля. Выпуск без посчитанной
        дюрации не выбрасывает остальные, но уменьшает покрытие — отчёт
        показывает его рядом, чтобы «2,8 года» нельзя было прочитать как
        характеристику всего портфеля.
        """
        weights: dict[str, tuple[float | None, float]] = {}
        total = self.total_value
        for holding in self._holdings:
            share = holding.value / total if total > 0 else 0.0
            ytm_result = calculate_ytm(holding.issue, self.as_of)
            ytm = ytm_result.ytm if ytm_result.ok else None
            weights[holding.issue.isin] = (
                macaulay_duration(holding.issue, self.as_of, ytm),
                share,
            )
        return portfolio_duration(weights), duration_coverage(weights)

    def weighted_ytm(self) -> float | None:
        """Средневзвешенная доходность к погашению — ориентир, не обещание.

        Бумаги погашаются в разные даты, а ставки реинвестирования заранее
        неизвестны, поэтому это число нельзя читать как будущую доходность
        портфеля. В отчёт оно идёт именно с такой оговоркой.
        """
        weighted = 0.0
        covered = 0.0
        for holding in self._holdings:
            result = calculate_ytm(holding.issue, self.as_of)
            if result.ok and result.ytm is not None and holding.value > 0:
                weighted += result.ytm * holding.value
                covered += holding.value
        if covered <= 0:
            return None
        return weighted / covered

    # --- проекция сделки ------------------------------------------------

    def with_units(self, issue: BondIssue, quantity: int) -> "PortfolioState":
        """Новый снимок после покупки `quantity` бумаг — без лотов.

        Отдельный метод нужен проверке лимитов: она может спросить про
        количество, не кратное лоту (например, «одна бумага»), и получать из-за
        этого другую сделку было бы ошибкой. Здесь количество берётся как есть.
        """
        if quantity <= 0:
            return self
        dirty = issue.dirty_price()
        if dirty is None:
            raise ValueError(f"{issue.secid}: нет цены или НКД, покупка не оценивается")
        amount = quantity * dirty / 100.0 * issue.nominal

        existing = self.by_isin().get(issue.isin)
        if existing is not None:
            new_holdings = [
                replace(h, quantity=h.quantity + quantity) if h.issue.isin == issue.isin else h
                for h in self._holdings
            ]
        else:
            new_holdings = list(self._holdings) + [Holding(issue, quantity)]

        return PortfolioState(
            as_of=self.as_of,
            cash=self.cash - amount,
            holdings=new_holdings,
            issuer_groups=self._issuer_groups,
            band_of=self._band_of,
            horizon_years=self.horizon_years,
            low_rating_levels=self._low_rating_levels,
        )

    def with_purchase(
        self, issue: BondIssue, lots: int, lot_size: int | None = None
    ) -> "PortfolioState":
        """Новый снимок после покупки `lots` лотов.

        `lot_size` по умолчанию берётся из выпуска. Количество переводится в
        бумаги здесь и только здесь: выше по коду лоты и бумаги не смешиваются.
        Деньги списываются по грязной цене, потому что НКД продавцу платится
        вместе с ценой.
        """
        if lots <= 0:
            return self
        size = lot_size if lot_size is not None else max(issue.min_lot, 1)
        return self.with_units(issue, lots * size)

    def purchase_cost(self, issue: BondIssue, lots: int, lot_size: int | None = None) -> float:
        """Стоимость `lots` лотов с НКД — без изменения состояния."""
        size = lot_size if lot_size is not None else max(issue.min_lot, 1)
        dirty = issue.dirty_price()
        if dirty is None:
            return float("inf")
        return lots * size * dirty / 100.0 * issue.nominal

    def __repr__(self) -> str:  # pragma: no cover - отладка
        return (
            f"PortfolioState(as_of={self.as_of}, cash={self.cash:.2f}, "
            f"holdings={len(self._holdings)}, value={self.total_value:.2f})"
        )
