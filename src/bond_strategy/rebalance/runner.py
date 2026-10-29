"""Точка входа ребалансировки: снимок, план и отчёт в один прогон.

Порядок шагов повторяет раздел 5 и не переставляется:

1. состояние — стоимость с НКД, доли, дюрация, купоны;
2. статусы действующих позиций;
3. доступные деньги — это `state.cash`;
4. допуск выпусков — внутри `plan`;
5. выбор покупок — там же;
6. распределение денег по лотам — цикл в `plan`.

Деньги на шаге 3 не вычисляются заново намеренно. Если бы доступный остаток
считался как «кэш + ожидаемые купоны до горизонта», планировщик тратил бы
деньги, которых ещё нет, и предлагал покупки на сумму больше счёта. В первой
версии реинвестируются только уже поступившие выплаты: купоны, амортизация и
погашения попадают в остаток тогда, когда зачислены.
"""
from __future__ import annotations

from datetime import date

from ..models import BondIssue
from .config import RebalanceConfig
from .planner import PlanResult, plan
from .report import RebalanceReport, build_report
from .snapshot import Holding, PortfolioState


def build_state(
    as_of: date,
    cash: float,
    holdings: list[tuple[BondIssue, int]],
    cfg: RebalanceConfig,
) -> PortfolioState:
    """Снимок портфеля по конфигу.

    `holdings` — пары (выпуск, количество бумаг). Количество в бумагах, а не в
    лотах: лот — свойство рынка, и перевод в него происходит только при покупке.
    """
    return PortfolioState.from_config(
        as_of=as_of,
        cash=cash,
        holdings=[Holding(issue, quantity) for issue, quantity in holdings],
        cfg=cfg,
    )


def rebalance(
    as_of: date,
    cash: float,
    holdings: list[tuple[BondIssue, int]],
    universe: list[BondIssue],
    cfg: RebalanceConfig,
) -> tuple[RebalanceReport, PlanResult]:
    """Полный прогон ребалансировки.

    `holdings` — действующие позиции. `universe` — выпуски, доступные на рынке.
    Портфель может содержать бумаги, которых в юниверсе нет: они не участвуют в
    покупках, но и не теряются из отчёта.
    """
    state = build_state(as_of, cash, holdings, cfg)
    plan_result = plan(state, universe, cfg)
    report = build_report(
        as_of=as_of,
        state=state,
        plan_result=plan_result,
        cfg=cfg,
        after_state=plan_result.after_state or state,
    )
    return report, plan_result
