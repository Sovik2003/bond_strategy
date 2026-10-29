"""Ребалансировка портфеля корпоративных облигаций (раздел 5 ТЗ).

Этапы (каждый — отдельный коммит):

    1. config, duration   — пороги и расчёт дюрации;
    2. snapshot           — состояние портфеля и проекция сделки;
    3. eligibility        — допуск выпуска к покупке (раздел 3);
    4. limits             — лимиты концентрации (раздел 2.1);
    5. positions          — статусы действующих позиций (шаг 2);
    6. planner            — жадный подбор покупок (шаги 4–6);
    7. report, runner     — отчёт «до/после» и сборка прогона.

Модуль отдельный от скринера: скринер отвечает «что интересно купить»,
ребалансировка — «что купить и в каком количестве на уже собранном портфеле».
"""
from .config import LadderBand, Limits, RebalanceConfig, load_rebalance_config
from .eligibility import Check, Eligibility, check_eligibility, check_risk_quality
from .limits import LimitCheck, check_purchase, existing_breaches, violations
from .planner import PlanResult, Purchase, Rejected, plan
from .positions import PositionAssessment, PositionStatus, assess_all
from .report import RebalanceReport, build_report
from .runner import build_state, rebalance
from .snapshot import Exposure, Holding, PortfolioState

__all__ = [
    "LadderBand",
    "Limits",
    "RebalanceConfig",
    "load_rebalance_config",
    "Exposure",
    "Holding",
    "PortfolioState",
    "Check",
    "Eligibility",
    "check_eligibility",
    "check_risk_quality",
    "LimitCheck",
    "check_purchase",
    "existing_breaches",
    "violations",
    "PositionAssessment",
    "PositionStatus",
    "assess_all",
    "PlanResult",
    "Purchase",
    "Rejected",
    "plan",
    "RebalanceReport",
    "build_report",
    "build_state",
    "rebalance",
]
