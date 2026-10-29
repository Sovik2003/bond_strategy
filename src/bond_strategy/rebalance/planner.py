"""Планировщик покупок: шаги 4–6 раздела 5.

Почему жадный алгоритм, а не оптимизация. Задача формально похожа на
целочисленное программирование, но у неё есть свойство, которое делает
оптимизатор вредным: критерии шага 5 заданы как приоритеты, а не как веса.
«Сначала уменьшить концентрацию, потом заполнить интервал сроков, потом
рейтинг, потом купоны, и только при равенстве — доходность» — это
лексикографический порядок. Если перевести его в веса, бумага с высокой
доходностью начнёт перебивать снижение концентрации, то есть ровно то, что
требование запрещает: «доходность учитывается, но не перекрывает обязательные
ограничения по риску». Поэтому перебор идёт по приоритетам, а не по сумме.

Сравнение кандидатов идёт с допуском (`concentration_epsilon`). Без допуска
победителем становился бы кандидат, отличающийся на тысячную процентного
пункта, и выбор был бы произвольным — а отчёт утверждал бы, что причина в
критерии.

Жадность здесь не приближение, а требование. Шаг 6 говорит: предложить
покупку, пересчитать состояние, повторить для оставшихся денег. Это и есть
жадный цикл. Остановка — по одному из условий требования, и каждое попадает в
отчёт отдельной причиной.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..analytics.cashflows import CashFlow, build_schedule
from ..analytics.duration import macaulay_duration
from ..analytics.yield_calc import calculate_ytm
from ..ingestion.ratings import _RANK
from ..models import BondIssue
from .config import RebalanceConfig
from .eligibility import check_eligibility
from .limits import check_purchase, violations
from .snapshot import PortfolioState

# Порог «заметной» выгоды. Один и тот же для концентрации, интервалов сроков и
# равномерности купонов: сравниваются доли портфеля, и разница меньше тысячной
# не должна объявляться причиной выбора.
EPS = 0.001


@dataclass
class Purchase:
    isin: str
    secid: str
    security_name: str | None
    issuer_name: str
    industry: str
    lots: int
    quantity: int
    amount: float
    rating: str | None
    band_code: str | None
    years_to_maturity: float | None
    duration: float | None
    ytm: float | None
    share_before: float
    share_after: float
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "isin": self.isin,
            "secid": self.secid,
            "security_name": self.security_name,
            "issuer": self.issuer_name,
            "industry": self.industry,
            "lots": self.lots,
            "quantity": self.quantity,
            "amount": round(self.amount, 2),
            "rating": self.rating,
            "band": self.band_code,
            "years_to_maturity": (
                None if self.years_to_maturity is None else round(self.years_to_maturity, 3)
            ),
            "duration": None if self.duration is None else round(self.duration, 4),
            "ytm": None if self.ytm is None else round(self.ytm, 6),
            "share_before": round(self.share_before, 6),
            "share_after": round(self.share_after, 6),
            "reasons": self.reasons,
        }


@dataclass
class Rejected:
    isin: str
    secid: str
    security_name: str | None
    codes: list[str]
    reason: str

    def as_dict(self) -> dict:
        return {
            "isin": self.isin,
            "secid": self.secid,
            "security_name": self.security_name,
            "codes": self.codes,
            "reason": self.reason,
        }


@dataclass
class PlanResult:
    as_of: date
    config_id: str
    purchases: list[Purchase] = field(default_factory=list)
    rejected: list[Rejected] = field(default_factory=list)
    stop_reason: str = ""
    cash_left: float = 0.0
    diagnostics: list[str] = field(default_factory=list)


class _FlowCache:
    """Графики выплат по ISIN.

    Купонный профиль пересчитывается на каждом шаге жадного цикла, а график
    выпуска от портфеля не зависит. Без кэша один и тот же build_schedule
    вызывался бы сотни раз на одной и той же бумаге.
    """

    def __init__(self, as_of: date) -> None:
        self.as_of = as_of
        self._schedules: dict[str, list[CashFlow]] = {}

    def get(self, issue: BondIssue) -> list[CashFlow]:
        if issue.isin not in self._schedules:
            try:
                self._schedules[issue.isin] = build_schedule(issue, self.as_of)
            except ValueError:
                self._schedules[issue.isin] = []
        return self._schedules[issue.isin]


def _ytm(issue: BondIssue, as_of: date) -> float | None:
    result = calculate_ytm(issue, as_of)
    return result.ytm if result.ok else None


def _coupon_profile(state: PortfolioState, flows: _FlowCache) -> dict[str, float]:
    out: dict[str, float] = {}
    for holding in state.holdings:
        for flow in flows.get(holding.issue):
            key = flow.payment_date.strftime("%Y-%m")
            out[key] = out.get(key, 0.0) + flow.amount_per_bond * holding.quantity
    return out


def _smoothness(profile: dict[str, float]) -> float:
    """Разброс купонных поступлений по месяцам: меньше — ровнее.

    Считается коэффициент вариации по месяцам, где выплаты вообще есть. Нули не
    учитываются: у бумаг с полугодовым купоном половина месяцев пустая, и
    включение нулей штрафовало бы любую такую бумагу одинаково, то есть критерий
    перестал бы различать кандидатов.
    """
    values = [v for v in profile.values() if v > 0]
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    if mean <= 0:
        return 0.0
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    return (variance ** 0.5) / mean


def _band_deviation(state: PortfolioState) -> float:
    """Суммарное квадратичное отклонение структуры сроков от целевой.

    Считается по всем интервалам, у которых задана цель. Последний интервал
    («более 5 лет») цели не имеет — там только верхний предел, и отклоняться от
    него некуда.

    Почему квадрат, а не модуль: дефицит в пустом интервале должен весить
    больше, чем недобор в почти заполненном. При линейной мере покупка на один
    лот одинаково «улучшала» бы оба случая, и критерий перестал бы отличать
    пустой интервал от заполненного — то есть первый смысл требования
    («заполняют недостающий интервал») потерялся бы.
    """
    exposure = state.exposure()
    total = 0.0
    for band in state.bands():
        if band.target is None:
            continue
        gap = band.target - exposure.band.get(band.code, 0.0)
        if gap > 0:
            total += gap * gap
    return total


def _worst_share(state: PortfolioState) -> float:
    """Худшая доля по отрасли и по группе: чем меньше, тем лучше.

    Почему не «снижение относительно текущего». Покупка переводит деньги в
    бумаги, стоимость портфеля от этого не меняется, поэтому доля уже крупной
    отрасли при покупке в ДРУГОЙ отрасли остаётся прежней, а не падает. Если
    считать выгодой только падение, критерий всегда давал бы ноль и не
    различал бы кандидатов — то есть первый приоритет шага 5 не работал бы
    вовсе.

    Сравнивать нужно результат: кандидат, после которого худшая доля ниже,
    лучше. Покупка в уже перегруженную отрасль поднимает эту долю и проигрывает
    покупке в свободную, которая её не меняет.
    """
    exposure = state.exposure()
    industry = max(exposure.industry.values(), default=0.0)
    group = max(exposure.group.values(), default=0.0)
    return max(industry, group)


def _max_lots(state: PortfolioState, issue: BondIssue, cfg: RebalanceConfig) -> int:
    """Сколько лотов можно купить, не нарушив лимиты и не превысив остаток.

    Сначала верхняя оценка по деньгам, потом спуск до первого допустимого
    количества. Спуск, а не поиск максимума: доля считается от меняющегося
    знаменателя, и нарушение не монотонно по количеству, поэтому бинарный поиск
    на этом сломался бы.
    """
    dirty = issue.dirty_price()
    if dirty is None or issue.min_lot <= 0:
        return 0
    lot_cost = issue.min_lot * dirty / 100.0 * issue.nominal
    if lot_cost <= 0:
        return 0
    by_cash = int(state.cash // lot_cost)
    if by_cash <= 0:
        return 0
    for lots in range(by_cash, 0, -1):
        after = state.with_purchase(issue, lots, issue.min_lot)
        if not violations(check_purchase(state, issue, lots * issue.min_lot, cfg, after)):
            return lots
    return 0


def _duration_allows(
    state: PortfolioState,
    issue: BondIssue,
    issue_duration: float | None,
    cfg: RebalanceConfig,
) -> tuple[bool, str]:
    """Правило дюрации из раздела 4 для новой покупки.

    Два условия:
      — дюрация отдельной бумаги не выше предела покупки;
      — если дюрация портфеля уже выше предела, покупка должна её снижать.

    Само превышение дюрации не вызывает продажу: это ставка, а не кредитный
    риск. Поэтому проверка стоит здесь, а не в статусах позиций.
    """
    if issue_duration is not None and issue_duration > cfg.duration_buy_max:
        return False, (
            f"дюрация {issue_duration:.2f} года выше предела покупки {cfg.duration_buy_max:g}"
        )

    portfolio_duration, _coverage = state.duration()
    if portfolio_duration is not None and portfolio_duration > cfg.duration_buy_max:
        if issue_duration is None or issue_duration >= portfolio_duration:
            return False, (
                f"дюрация портфеля {portfolio_duration:.2f} года выше {cfg.duration_buy_max:g}: "
                "разрешены только покупки, снижающие её"
            )
    return True, ""


@dataclass
class _Candidate:
    issue: BondIssue
    lots: int
    amount: float
    ytm: float | None
    duration: float | None
    rating_rank: int
    concentration_score: float
    band_gain: float
    smoothness_gain: float
    share_before: float
    share_after: float
    reasons: list[str] = field(default_factory=list)


def _score_candidate(
    state: PortfolioState,
    issue: BondIssue,
    lots: int,
    as_of: date,
    cfg: RebalanceConfig,
    flows: _FlowCache,
) -> _Candidate:
    """Собирает кандидата вместе с обоснованием по критериям шага 5."""
    amount = state.purchase_cost(issue, lots, issue.min_lot)
    after = state.with_purchase(issue, lots, issue.min_lot)
    ytm = _ytm(issue, as_of)
    duration = macaulay_duration(issue, as_of, ytm)
    band = state.band_of(issue)

    reasons: list[str] = []

    # Знак минус: меньше худшая доля — лучше кандидат, а сравнение идёт по
    # принципу «больше значит лучше», как у остальных критериев.
    concentration_score = -_worst_share(after)
    if concentration_score > -_worst_share(state) + EPS:
        reasons.append("снижает концентрацию по отрасли или группе компаний")

    # Уменьшение отклонения структуры сроков от целевой: больше — лучше.
    band_gain = _band_deviation(state) - _band_deviation(after)
    if band_gain > EPS and band is not None and band.target is not None:
        reasons.append(f"заполняет недостающий интервал сроков «{band.label}»")

    smoothness_gain = _smoothness(_coupon_profile(state, flows)) - _smoothness(
        _coupon_profile(after, flows)
    )
    if smoothness_gain > EPS:
        reasons.append("выравнивает ожидаемые купонные выплаты")

    existing = state.by_isin().get(issue.isin)
    share_before = state.share(existing.value) if existing else 0.0
    new_holding = after.by_isin().get(issue.isin)
    share_after = after.share(new_holding.value) if new_holding else 0.0

    return _Candidate(
        issue=issue,
        lots=lots,
        amount=amount,
        ytm=ytm,
        duration=duration,
        rating_rank=_RANK.get(issue.rating or "", len(_RANK)),
        concentration_score=concentration_score,
        band_gain=band_gain,
        smoothness_gain=smoothness_gain,
        share_before=share_before,
        share_after=share_after,
        reasons=reasons,
    )


def _better(a: _Candidate, b: _Candidate, epsilon: float) -> bool:
    """Лексикографическое сравнение по приоритетам шага 5.

    Порядок критериев взят из требования и не переставляется:
      1) уменьшение существенной концентрации по группе и отрасли;
      2) заполнение недостающего интервала сроков;
      3) более высокий рейтинг при сопоставимой роли;
      4) лучшее распределение купонных выплат;
      5) более высокая доходность к погашению — только при равенстве.

    Доходность стоит последней намеренно: иначе она перебивала бы снижение
    концентрации, то есть ровно то, что требование запрещает.
    """
    if a.concentration_score > b.concentration_score + epsilon:
        return True
    if b.concentration_score > a.concentration_score + epsilon:
        return False

    if a.band_gain > b.band_gain + epsilon:
        return True
    if b.band_gain > a.band_gain + epsilon:
        return False

    if a.rating_rank != b.rating_rank:
        return a.rating_rank < b.rating_rank  # меньший индекс — лучший рейтинг

    if a.smoothness_gain > b.smoothness_gain + epsilon:
        return True
    if b.smoothness_gain > a.smoothness_gain + epsilon:
        return False

    ay = a.ytm if a.ytm is not None else float("-inf")
    by = b.ytm if b.ytm is not None else float("-inf")
    if abs(ay - by) > epsilon:
        return ay > by

    # Полное равенство: порядок по SECID, чтобы один и тот же портфель давал
    # один и тот же отчёт при повторном запуске.
    return a.issue.secid < b.issue.secid


def plan(
    state: PortfolioState,
    universe: list[BondIssue],
    cfg: RebalanceConfig,
) -> PlanResult:
    """Жадный план покупок на доступный остаток.

    `universe` — выпуски, доступные на рынке. Портфель может содержать бумаги,
    которых в юниверсе нет; они не участвуют в покупках, но и не теряются.
    """
    result = PlanResult(as_of=state.as_of, config_id=cfg.config_id)
    flows = _FlowCache(state.as_of)

    # Допуск проверяется один раз: свойства выпуска от денег не зависят.
    eligible: list[BondIssue] = []
    for issue in universe:
        verdict = check_eligibility(issue, cfg, state.as_of)
        if verdict.passed:
            eligible.append(issue)
        else:
            result.rejected.append(
                Rejected(
                    isin=issue.isin,
                    secid=issue.secid,
                    security_name=issue.security_name,
                    codes=verdict.codes,
                    reason=verdict.reason,
                )
            )

    if not eligible:
        result.stop_reason = "Нет выпусков, проходящих требования к покупке (раздел 3)."
        result.cash_left = state.cash
        return result

    current = state
    for _step in range(cfg.max_purchases):
        if current.total_value > 0 and current.cash <= 0:
            result.stop_reason = "Денежный остаток исчерпан."
            break

        best: _Candidate | None = None
        duration_blocked = 0

        for issue in eligible:
            lots = _max_lots(current, issue, cfg)
            if lots <= 0:
                continue
            issue_duration = macaulay_duration(issue, state.as_of, _ytm(issue, state.as_of))
            allowed, why = _duration_allows(current, issue, issue_duration, cfg)
            if not allowed:
                duration_blocked += 1
                if why not in result.diagnostics:
                    result.diagnostics.append(f"{issue.secid}: {why}")
                continue

            candidate = _score_candidate(current, issue, lots, state.as_of, cfg, flows)
            if best is None or _better(candidate, best, cfg.concentration_epsilon):
                best = candidate

        if best is None:
            # Порядок причин: сначала «денег не хватает на лот», потому что это
            # самое частое состояние на малом портфеле, и инвестору нужно
            # понимать разницу между «денег мало» и «бумаги не подходят».
            affordable_lot = None
            for issue in eligible:
                cost = current.purchase_cost(issue, 1, issue.min_lot)
                if cost < float("inf"):
                    affordable_lot = cost if affordable_lot is None else min(affordable_lot, cost)

            if affordable_lot is not None and current.cash < affordable_lot:
                result.stop_reason = (
                    "Доступного остатка недостаточно для приобретения подходящего лота: "
                    f"минимальный лот {affordable_lot:,.0f} ₽ при остатке {current.cash:,.0f} ₽. "
                    "Деньги остаются на счёте."
                )
            elif duration_blocked and duration_blocked == len(eligible):
                result.stop_reason = (
                    "Все подходящие выпуски отсеяны правилом дюрации раздела 4."
                )
            else:
                result.stop_reason = (
                    "Покупки не предложены: следующая покупка создала бы новое нарушение "
                    "ограничений."
                )
            break

        after = current.with_purchase(best.issue, best.lots, best.issue.min_lot)
        if violations(check_purchase(current, best.issue, best.lots * best.issue.min_lot, cfg, after)):
            # Кандидат прошёл предварительную проверку, но итоговая не сошлась.
            # Продолжать нельзя: следующая итерация выбрала бы его же.
            result.stop_reason = (
                f"Покупка {best.issue.secid} создала бы нарушение ограничений; план остановлен."
            )
            break

        if not best.reasons:
            best.reasons.append("лучшая доходность среди сопоставимых кандидатов")
        band = current.band_of(best.issue)
        result.purchases.append(
            Purchase(
                isin=best.issue.isin,
                secid=best.issue.secid,
                security_name=best.issue.security_name,
                issuer_name=best.issue.issuer_name,
                industry=best.issue.industry,
                lots=best.lots,
                quantity=best.lots * max(best.issue.min_lot, 1),
                amount=best.amount,
                rating=best.issue.rating,
                band_code=band.code if band else None,
                years_to_maturity=current.years_to_maturity(best.issue),
                duration=best.duration,
                ytm=best.ytm,
                share_before=best.share_before,
                share_after=best.share_after,
                reasons=best.reasons,
            )
        )
        current = after
    else:
        result.stop_reason = f"Достигнут предел числа покупок за запуск ({cfg.max_purchases})."

    result.cash_left = current.cash
    if not result.purchases and not result.stop_reason:
        result.stop_reason = (
            "Продажа не рекомендуется. Покупки не предложены: подходящих выпусков нет."
        )
    return result
