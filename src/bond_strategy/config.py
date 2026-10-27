"""Загрузка и версионирование конфигурации стратегии."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class StrategyConfig:
    raw: dict[str, Any]
    config_id: str

    def get(self, *path: str, default: Any = None) -> Any:
        node: Any = self.raw
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    @property
    def initial_capital(self) -> float:
        return float(self.get("capital", "initial"))

    @property
    def reserve_base(self) -> float:
        return float(self.get("capital", "reserve_base"))

    @property
    def min_position(self) -> float:
        return float(self.get("filters", "min_position"))

    @property
    def min_buy_score(self) -> float:
        return float(self.get("signals", "min_buy_score"))

    @property
    def cooldown_days(self) -> int:
        return int(self.get("signals", "duplicate_cooldown_days"))


DEFAULT_CONFIG: dict[str, Any] = {
    "version": "1.1.0",
    "capital": {
        "initial": 50000,
        "reserve_base": 10000,
        "reserve_conservative": 20000,
        "reserve_income": 8000,
    },
    "limits": {
        "issuer_max_share": 0.25,
        "issue_max_share": 0.25,
        "industry_max_share": 0.40,
        "medium_quality_max_share": 0.25,
        "max_long_maturity_share": 0.20,
    },
    "filters": {
        "min_position": 8000,
        "min_daily_turnover": 300000,
        "max_spread_core": 0.005,
        "max_spread_medium": 0.01,
        "min_days_to_maturity": 180,
        "preferred_min_days": 270,
        "preferred_max_years": 3,
        "max_bonds_total": 0,
    },
    "signals": {
        "min_buy_score": 80,
        "core_premium_min": 0.01,
        "medium_premium_min": 0.02,
        "duplicate_cooldown_days": 5,
    },
    "scoring_weights": {
        "credit": 0.35,
        "financial": 0.20,
        "yield": 0.20,
        "liquidity": 0.15,
        "structure": 0.10,
    },
    "freshness": {
        "price_max_age_days": 2,
        "rating_max_age_days": 400,
        "turnover_max_age_days": 5,
    },
}


def build_config(data: dict[str, Any]) -> StrategyConfig:
    canonical = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return StrategyConfig(raw=data, config_id=hashlib.sha256(canonical.encode()).hexdigest()[:12])


def load_config(path: str | Path | None = None) -> StrategyConfig:
    """Читает конфиг из файла, иначе использует встроенный DEFAULT_CONFIG."""
    if path is None:
        path = Path("config/strategy.json")
    path = Path(path)
    if path.exists():
        return build_config(json.loads(path.read_text(encoding="utf-8")))
    return build_config(DEFAULT_CONFIG)
