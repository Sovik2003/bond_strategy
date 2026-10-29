"""Лимиты концентрации — раздел 2.1, с правилом «не ухудшать».

Главная тонкость модуля. Наивная проверка «доля после покупки не выше лимита»
ломается на уже собранном портфеле: если одна отрасль вышла за 25% из-за роста
цены, такая проверка запретит любую покупку вообще, включая бумагу другой
отрасли, которая концентрацию уменьшает. Требование говорит обратное:

    «если, например, одна отрасль уже выше лимита, это не должно блокировать
     покупку бумаги другой отрасли, которая уменьшает концентрацию»

Поэтому сравнение идёт не с лимитом, а с текущим состоянием: доля после сделки
должна быть не больше лимита ИЛИ не больше, чем была до сделки. Второе условие
и есть «не ухудшать»: выйти из нарушения одной покупкой нельзя, но и усугубить
его нельзя тоже.

Отсюда два разных понятия в отчёте:

    override — формальное превышение лимита (может быть и до сделки);
    violation — превышение, которое создаёт или увеличивает ЭТА покупка.

Покупать при существующем превышении можно, если сделка его не растит. Нельзя
покупать, если она создаёт новое превышение.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..models import BondIssue
from .config import RebalanceConfig
from .snapshot import PortfolioState

# Допуск на погрешность вычислений с плавающей точкой. Без него доля
# 0.050000000001 считалась бы нарушением лимита 5% — а она получается простым
# делением двух чисел.
EPS = 1e-9


@dataclass(frozen=True)
class LimitCheck:
    code: str
    label: str
    limit: float
    before: float
    after: float
    key: str = ""

    @property
    def exceeds(self) -> bool:
        """Доля после сделки выше лимита."""
        return self.after > self.limit + EPS

    @property
    def preexisting(self) -> bool:
        """Лимит был нарушен ещё до сделки."""
        return self.before > self.limit + EPS

    @property
    def worsened(self) -> bool:
        """Сделка увеличивает превышение или создаёт новое."""
        return self.after > self.before + EPS

    @property
    def blocks(self) -> bool:
        """Покупка недопустима: превышение есть и оно не уменьшилось.

        Снижение доли, которая уже выше лимита, улучшением считается, даже
        если порог формально не достигнут — иначе из нарушения нельзя было бы
        выйти покупками.
        """
        return self.exceeds and self.worsened

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "label": self.label,
            "limit": round(self.limit, 6),
            "before": round(self.before, 6),
            "after": round(self.after, 6),
            "key": self.key,
            "blocks": self.blocks,
        }


def _compare(
    code: str,
    label: str,
    limit: float,
    before: float,
    after: float,
    key: str = "",
) -> LimitCheck:
    return LimitCheck(code=code, label=label, limit=limit, before=before, after=after, key=key)


def check_purchase(
    state: PortfolioState,
    issue: BondIssue,
    quantity: int,
    cfg: RebalanceConfig,
    after: PortfolioState | None = None,
) -> list[LimitCheck]:
    """Все лимиты для покупки `quantity` бумаг.

    `quantity` — в бумагах, не в лотах. `after` можно передать готовым, если
    состояние после сделки уже посчитано: планировщик строит его для оценки
    кандидата, и пересчитывать второй раз незачем.

    Возвращаются ВСЕ проверки, включая пройденные: отчёт показывает таблицу
    «до/после» по каждому лимиту. Без этого не видно запаса до порога и
    непонятно, почему следующая покупка не предложена.
    """
    if quantity <= 0:
        return []

    if after is None:
        # Ровно `quantity` бумаг: проверка лимитов не обязана быть кратна лоту,
        # и округление до лота дало бы проверку другой сделки.
        after = state.with_units(issue, quantity)

    limits = cfg.limits
    before_exp = state.exposure()
    after_exp = after.exposure()
    group_key = state.group_of(issue)

    return [
        _compare(
            "ISSUE_LIMIT",
            "Один выпуск",
            limits.issue_max_share,
            before_exp.issue.get(issue.isin, 0.0),
            after_exp.issue.get(issue.isin, 0.0),
            key=issue.isin,
        ),
        _compare(
            "ISSUER_LIMIT",
            "Один эмитент",
            limits.issuer_max_share,
            before_exp.issuer.get(issue.issuer_id, 0.0),
            after_exp.issuer.get(issue.issuer_id, 0.0),
            key=issue.issuer_id,
        ),
        _compare(
            "GROUP_LIMIT",
            "Группа связанных компаний",
            limits.group_max_share,
            before_exp.group.get(group_key, 0.0),
            after_exp.group.get(group_key, 0.0),
            key=group_key,
        ),
        _compare(
            "INDUSTRY_LIMIT",
            "Одна отрасль",
            limits.industry_max_share,
            before_exp.industry.get(issue.industry, 0.0),
            after_exp.industry.get(issue.industry, 0.0),
            key=issue.industry,
        ),
        # Лимиты категории и срока считаются от доли ВСЕГО портфеля в группе, а
        # не от доли покупаемого выпуска. Поэтому покупка бумаги другой
        # категории их не увеличивает: after == before, и проверка проходит
        # сама. Отдельного условия «считать только для своей категории» не
        # нужно, и оно было бы вредным — тогда в отчёте не было бы видно, что
        # лимит уже нарушен чужими бумагами.
        _compare(
            "LOW_RATING_LIMIT",
            f"Категория {'/'.join(sorted(cfg.low_rating_levels))}",
            limits.low_rating_max_share,
            before_exp.low_rating,
            after_exp.low_rating,
        ),
        _compare(
            "OVER_HORIZON_LIMIT",
            f"Погашение более чем через {cfg.horizon_years:g} лет",
            limits.over_5y_max_share,
            before_exp.over_horizon,
            after_exp.over_horizon,
        ),
    ]


def violations(checks: list[LimitCheck]) -> list[LimitCheck]:
    """Проверки, из-за которых покупка недопустима."""
    return [c for c in checks if c.blocks]


def existing_breaches(state: PortfolioState, cfg: RebalanceConfig) -> dict[str, tuple[str, float]]:
    """Превышения, которые уже есть в портфеле — до всяких покупок.

    Нужны отчёту отдельно от проверок сделки: «портфель уже выше лимита по
    отрасли» и «эта покупка выводит отрасль за лимит» — разные утверждения, и
    смешивать их значит обвинять сделку в том, что было раньше.
    """
    limits = cfg.limits
    exposure = state.exposure()
    out: dict[str, tuple[str, float]] = {}

    def mark(code: str, label: str, value: float, limit: float, key: str = "") -> None:
        if value > limit + EPS:
            keyed = f"{code}:{key}" if key else code
            out[keyed] = (f"{label} — {value:.1%} при лимите {limit:.0%}", value)

    for isin, share in exposure.issue.items():
        mark("ISSUE_LIMIT", f"Выпуск {isin}", share, limits.issue_max_share, isin)
    for issuer_id, share in exposure.issuer.items():
        mark("ISSUER_LIMIT", f"Эмитент {issuer_id}", share, limits.issuer_max_share, issuer_id)
    for gid, share in exposure.group.items():
        mark("GROUP_LIMIT", f"Группа {gid}", share, limits.group_max_share, gid)
    for industry, share in exposure.industry.items():
        mark("INDUSTRY_LIMIT", f"Отрасль «{industry}»", share, limits.industry_max_share, industry)

    mark("LOW_RATING_LIMIT", "Категория BBB/BBB+", exposure.low_rating, limits.low_rating_max_share)
    mark(
        "OVER_HORIZON_LIMIT",
        f"Срок более {cfg.horizon_years:g} лет",
        exposure.over_horizon,
        limits.over_5y_max_share,
    )
    return out
