"""Конфигурация ребалансировки.

Отдельный файл от `strategy.json`: скринер отвечает на вопрос «что интересно
купить», ребалансировка — «что купить и в каком количестве на уже собранном
портфеле». Как и в основном конфиге, сохраняется `config_id`: отчёт должен быть
воспроизводим.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..ingestion.ratings import _RANK, RATING_SCALE


@dataclass(frozen=True)
class LadderBand:
    """Интервал оставшегося срока. target=None — интервал задаёт только верхний предел."""

    code: str
    label: str
    lower_years: float
    upper_years: float | None
    target: float | None
    lower: float
    upper: float

    def contains(self, years: float | None) -> bool:
        if years is None or years < 0:
            return False
        if years < self.lower_years:
            return False
        if self.upper_years is None:
            return True
        return years <= self.upper_years


@dataclass(frozen=True)
class Limits:
    issue_max_share: float
    issuer_max_share: float
    group_max_share: float
    industry_max_share: float
    low_rating_max_share: float
    over_5y_max_share: float


@dataclass(frozen=True)
class RebalanceConfig:
    raw: dict[str, Any]
    config_id: str
    bands: tuple[LadderBand, ...]
    limits: Limits
    rating_floor: str
    rating_floor_rank: int
    low_rating_levels: frozenset[str]
    rating_max_age_days: int
    approved_agencies: frozenset[str]
    horizon_years: float
    duration_target_min: float
    duration_target_max: float
    duration_buy_max: float
    concentration_epsilon: float
    max_purchases: int
    min_data_completeness: float
    require_unknown_flags_verified: bool
    require_fixed_coupon: bool
    require_maturity: bool
    reject_offer: bool
    groups: dict[str, str] = field(default_factory=dict)

    def band_for(self, years: float | None) -> LadderBand | None:
        for band in self.bands:
            if band.contains(years):
                return band
        return None

    def group_of(self, issuer_id: str) -> str:
        """Группа эмитента. Нет в карте — эмитент сам себе группа."""
        return self.groups.get(issuer_id, issuer_id)


def _low_rating_levels(floor_rank: int) -> frozenset[str]:
    """Уровни категории BBB/BBB+: BBB-префикс, рейтинг не ниже порога.

    Индекс меньше — рейтинг выше. Значит, уровень входит, если его индекс
    не больше индекса порога (BBB+ = 7, BBB = 8; порог BBB = 8).
    """
    return frozenset(
        level
        for level in RATING_SCALE
        if level.startswith("BBB") and _RANK[level] <= floor_rank
    )


def load_rebalance_config(path: str | Path) -> RebalanceConfig:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    canonical = json.dumps(data, sort_keys=True, ensure_ascii=False)
    config_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]

    limits_raw = data["limits"]
    limits = Limits(
        issue_max_share=float(limits_raw["issue_max_share"]),
        issuer_max_share=float(limits_raw["issuer_max_share"]),
        group_max_share=float(limits_raw["group_max_share"]),
        industry_max_share=float(limits_raw["industry_max_share"]),
        low_rating_max_share=float(limits_raw["low_rating_max_share"]),
        over_5y_max_share=float(limits_raw["over_5y_max_share"]),
    )

    ratings_raw = data["ratings"]
    floor = str(ratings_raw["min_level"]).strip().upper()
    if floor not in _RANK:
        raise ValueError(f"Неизвестный минимальный рейтинг: {floor}")

    bands = tuple(
        LadderBand(
            code=str(b["code"]),
            label=str(b["label"]),
            lower_years=float(b["lower_years"]),
            upper_years=None if b.get("upper_years") is None else float(b["upper_years"]),
            target=None if b.get("target") is None else float(b["target"]),
            lower=float(b.get("lower", 0.0)),
            upper=float(b["upper"]),
        )
        for b in data["ladder"]["bands"]
    )
    if not bands:
        raise ValueError("Не задан ни один интервал сроков")

    duration_raw = data["duration"]
    dmin = float(duration_raw["target_min"])
    dmax = float(duration_raw["target_max"])
    dbuy = float(duration_raw["buy_max"])
    if not dmin <= dmax <= dbuy:
        raise ValueError("Порядок дюрации нарушен: target_min <= target_max <= buy_max")

    eligibility = data.get("eligibility", {})
    planner = data.get("planner", {})
    groups_raw = (data.get("issuer_groups", {}) or {}).get("groups", {}) or {}

    return RebalanceConfig(
        raw=data,
        config_id=config_id,
        bands=bands,
        limits=limits,
        rating_floor=floor,
        rating_floor_rank=_RANK[floor],
        low_rating_levels=_low_rating_levels(_RANK[floor]),
        rating_max_age_days=int(ratings_raw.get("max_age_days", 400)),
        approved_agencies=frozenset(ratings_raw.get("approved_agencies", [])),
        horizon_years=float(data["horizon"]["years"]),
        duration_target_min=dmin,
        duration_target_max=dmax,
        duration_buy_max=dbuy,
        concentration_epsilon=float(planner.get("concentration_epsilon", 0.001)),
        max_purchases=int(planner.get("max_purchases", 200)),
        min_data_completeness=float(eligibility.get("min_data_completeness", 1.0)),
        require_unknown_flags_verified=bool(eligibility.get("require_unknown_flags_verified", False)),
        require_fixed_coupon=bool(eligibility.get("require_fixed_coupon", True)),
        require_maturity=bool(eligibility.get("require_maturity", True)),
        reject_offer=bool(eligibility.get("reject_offer", True)),
        groups={str(k): str(v) for k, v in groups_raw.items()},
    )
