"""Конфигурация с идентификатором версии.

Пороги живут здесь, а не в коде: расчёт сохраняет config_id, поэтому
историческое решение можно воспроизвести.
"""
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
    def broker_rate(self) -> float:
        return float(self.get("commissions", "broker_rate", default=0.0))


def load_config(path: str | Path) -> StrategyConfig:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    canonical = json.dumps(data, sort_keys=True, ensure_ascii=False)
    config_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return StrategyConfig(raw=data, config_id=config_id)
