"""Отчёт после запуска — раздел 6 требований.

Отчёт отвечает на два вопроса сразу, и они не должны слипаться:

    что сделать       — таблица рекомендаций: действие, выпуск, количество,
                        сумма, причина, риск;
    что стало со      — состояние портфеля до и после: доли выпусков,
    структурой          эмитентов, групп и отраслей, доля BBB/BBB+, сроки,
                        дюрация, остаток, ожидаемые купоны по месяцам.

Средневзвешенная доходность к погашению показывается как ориентир. Это не
обещание будущей доходности: бумаги погашаются в разные даты, а ставки
реинвестирования заранее неизвестны. Поэтому рядом с числом всегда стоит эта
оговорка — иначе «16% годовых» прочитается как гарантия.

Доходность в первой версии считается до налогов и комиссий. Это указано в
примечаниях отчёта, а не подразумевается: инвестор имеет право знать, что
видит валовую величину.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .config import RebalanceConfig
from .limits import existing_breaches
from .planner import PlanResult
from .positions import NOT_A_SELL_REASON, PositionAssessment, PositionStatus, assess_all
from .snapshot import Exposure, PortfolioState


def _slice_rows(
    code: str,
    name: str,
    before: dict[str, float],
    after: dict[str, float],
    limit: float | None = None,
    target: dict[str, float] | None = None,
    labels: dict[str, str] | None = None,
) -> dict:
    """Строки разреза: все ключи из обоих состояний, отсортированы по доле."""
    labels = labels or {}
    keys = list(dict.fromkeys(list(before) + list(after)))
    rows = [
        {
            "key": key,
            "label": labels.get(key, key),
            "before": round(before.get(key, 0.0), 6),
            "after": round(after.get(key, 0.0), 6),
            "limit": limit,
            "target": (target or {}).get(key),
            "over_limit": bool(limit is not None and after.get(key, 0.0) > limit + 1e-9),
        }
        for key in keys
    ]
    return {"code": code, "name": name, "limit": limit, "rows": sorted(
        rows, key=lambda r: (-r["after"], r["key"])
    )}


def _weighted_ytm(state: PortfolioState) -> float | None:
    return state.weighted_ytm()


def _rating_distribution(state: PortfolioState) -> dict[str, float]:
    total = state.total_value
    out: dict[str, float] = {}
    for holding in state.holdings:
        key = holding.issue.rating or "нет рейтинга"
        share = holding.value / total if total > 0 else 0.0
        out[key] = out.get(key, 0.0) + share
    return out


@dataclass
class RebalanceReport:
    as_of: date
    config_id: str
    positions: list[PositionAssessment] = field(default_factory=list)
    purchases: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    structure: list[dict] = field(default_factory=list)
    cash_before: float = 0.0
    cash_after: float = 0.0
    value_before: float = 0.0
    value_after: float = 0.0
    duration_before: float | None = None
    duration_after: float | None = None
    duration_coverage_before: float = 0.0
    duration_coverage_after: float = 0.0
    ytm_before: float | None = None
    ytm_after: float | None = None
    flows_before: dict = field(default_factory=dict)
    flows_after: dict = field(default_factory=dict)
    breaches: dict = field(default_factory=dict)
    summary: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    stop_reason: str = ""

    def as_dict(self) -> dict:
        return {
            "as_of": self.as_of.isoformat(),
            "config_id": self.config_id,
            "summary": self.summary,
            "positions": [p.as_dict() for p in self.positions],
            "purchases": self.purchases,
            "rejected": self.rejected,
            "structure": self.structure,
            "breaches": self.breaches,
            "cash": {"before": round(self.cash_before, 2), "after": round(self.cash_after, 2)},
            "value": {"before": round(self.value_before, 2), "after": round(self.value_after, 2)},
            "duration": {
                "before": self.duration_before,
                "after": self.duration_after,
                "coverage_before": round(self.duration_coverage_before, 4),
                "coverage_after": round(self.duration_coverage_after, 4),
            },
            "ytm_orientation": {"before": self.ytm_before, "after": self.ytm_after},
            "flows": {"before": self.flows_before, "after": self.flows_after},
            "notes": self.notes,
            "diagnostics": self.diagnostics,
            "stop_reason": self.stop_reason,
        }


def build_report(
    as_of: date,
    state: PortfolioState,
    plan_result: PlanResult,
    cfg: RebalanceConfig,
    *,
    after_state: PortfolioState | None = None,
) -> RebalanceReport:
    """Собирает отчёт «до/после» из снимка портфеля и плана покупок."""
    if after_state is None:
        # Проекция «после» приходит из планировщика: у новых выпусков объекта
        # в исходном снимке нет, поэтому восстановить её по одному ISIN нельзя.
        raise ValueError("build_report требует after_state — проекцию из планировщика")

    before_exp = state.exposure()
    after_exp = after_state.exposure()

    limit_labels = {
        "issue": cfg.limits.issue_max_share,
        "issuer": cfg.limits.issuer_max_share,
        "group": cfg.limits.group_max_share,
        "industry": cfg.limits.industry_max_share,
    }
    target_bands = {b.code: b.target for b in cfg.bands if b.target is not None}
    band_labels = {b.code: b.label for b in cfg.bands}

    structure = [
        _slice_rows("issue", "Выпуски", before_exp.issue, after_exp.issue, limit_labels["issue"]),
        _slice_rows("issuer", "Эмитенты", before_exp.issuer, after_exp.issuer, limit_labels["issuer"]),
        _slice_rows("group", "Группы компаний", before_exp.group, after_exp.group, limit_labels["group"]),
        _slice_rows(
            "industry", "Отрасли", before_exp.industry, after_exp.industry, limit_labels["industry"]
        ),
        _slice_rows(
            "band", "Сроки погашения", before_exp.band, after_exp.band, labels=band_labels
        ),
        {
            "code": "low_rating",
            "name": f"Категория {'/'.join(sorted(cfg.low_rating_levels))}",
            "limit": cfg.limits.low_rating_max_share,
            "rows": [
                {
                    "key": "low_rating",
                    "label": f"{'/'.join(sorted(cfg.low_rating_levels))}",
                    "before": round(before_exp.low_rating, 6),
                    "after": round(after_exp.low_rating, 6),
                    "limit": cfg.limits.low_rating_max_share,
                    "target": None,
                    "over_limit": after_exp.low_rating > cfg.limits.low_rating_max_share + 1e-9,
                }
            ],
        },
        {
            "code": "over_horizon",
            "name": f"Срок более {cfg.horizon_years:g} лет",
            "limit": cfg.limits.over_5y_max_share,
            "rows": [
                {
                    "key": "over_horizon",
                    "label": f"более {cfg.horizon_years:g} лет",
                    "before": round(before_exp.over_horizon, 6),
                    "after": round(after_exp.over_horizon, 6),
                    "limit": cfg.limits.over_5y_max_share,
                    "target": None,
                    "over_limit": after_exp.over_horizon > cfg.limits.over_5y_max_share + 1e-9,
                }
            ],
        },
    ]

    duration_before, coverage_before = state.duration()
    duration_after, coverage_after = after_state.duration()

    sells = [p for p in assess_all(state, cfg, as_of) if p.needs_sell]
    can_add = [p for p in assess_all(state, cfg, as_of) if not p.can_add]

    report = RebalanceReport(
        as_of=as_of,
        config_id=cfg.config_id,
        positions=assess_all(state, cfg, as_of),
        purchases=[p.as_dict() for p in plan_result.purchases],
        rejected=[r.as_dict() for r in plan_result.rejected],
        structure=structure,
        cash_before=state.cash,
        cash_after=after_state.cash,
        value_before=state.total_value,
        value_after=after_state.total_value,
        duration_before=duration_before,
        duration_after=duration_after,
        duration_coverage_before=coverage_before,
        duration_coverage_after=coverage_after,
        ytm_before=_weighted_ytm(state),
        ytm_after=_weighted_ytm(after_state),
        flows_before=state.coupon_profile(),
        flows_after=after_state.coupon_profile(),
        breaches={
            key: {"text": text, "value": round(value, 6)}
            for key, (text, value) in existing_breaches(state, cfg).items()
        },
        summary={
            "positions": len(state.holdings),
            "purchases": len(plan_result.purchases),
            "sell_candidates": len(sells),
            "do_not_add": len(can_add),
            "rejected": len(plan_result.rejected),
            "cash_left": round(after_state.cash, 2),
        },
        diagnostics=list(plan_result.diagnostics),
        stop_reason=plan_result.stop_reason,
    )
    report.notes = _build_notes(report, cfg, state, after_state, sells)
    return report


def _build_notes(
    report: RebalanceReport,
    cfg: RebalanceConfig,
    before: PortfolioState,
    after: PortfolioState,
    sells: list[PositionAssessment],
) -> list[str]:
    """Примечания, без которых числа читаются неправильно."""
    notes: list[str] = []

    notes.append(
        "Доходность в первой версии показана до налогов и комиссий: фактическая "
        "выгодность сделки может отличаться."
    )

    if report.ytm_before is not None or report.ytm_after is not None:
        notes.append(
            "Средневзвешенная доходность к погашению — ориентир, а не гарантированная "
            "будущая доходность: бумаги погашаются в разные даты, ставки "
            "реинвестирования заранее неизвестны."
        )

    if report.duration_before is not None and report.duration_coverage_before < 0.999:
        notes.append(
            f"Дюрация портфеля посчитана по {report.duration_coverage_before:.0%} стоимости: "
            "по остальным бумагам не хватает данных."
        )

    if sells:
        notes.append(
            "«Рассмотреть продажу» — рекомендация, а не сделка. После дефолта бумагу может "
            "быть сложно продать по приемлемой цене."
        )

    notes.append(NOT_A_SELL_REASON)

    if report.breaches:
        notes.append(
            "В портфеле уже есть превышения лимитов. Они не блокируют покупки бумаг "
            "других отраслей: правило — не ухудшать существующее превышение, а не "
            "запрещать любые сделки."
        )

    if report.cash_after > 0:
        notes.append(
            f"Остаток {report.cash_after:,.0f} ₽ сохраняется на счёте. "
            "Если выбор между нарушением ограничений и остатком, алгоритм оставляет деньги."
        )

    if cfg.duration_target_min and cfg.duration_target_max and report.duration_after is not None:
        if not (cfg.duration_target_min <= report.duration_after <= cfg.duration_target_max):
            notes.append(
                f"Дюрация после рекомендаций {report.duration_after:.2f} года вне ориентира "
                f"{cfg.duration_target_min:g}–{cfg.duration_target_max:g}. Это не повод "
                "продавать: превышение дюрации — ставка, а не кредитный риск."
            )

    return notes
