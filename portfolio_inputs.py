"""Validation and local persistence of manually entered portfolio holdings."""
from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path


def validate_portfolio(cash, positions):
    """Return a normalized snapshot; each value is total RUB including accrued interest."""
    if isinstance(cash, bool):
        raise ValueError("Свободные деньги должны быть неотрицательным числом")
    try:
        cash = float(cash)
    except (TypeError, ValueError):
        raise ValueError("Укажите свободные деньги") from None
    if not math.isfinite(cash) or cash < 0:
        raise ValueError("Свободные деньги должны быть неотрицательным числом")
    result = []
    seen = set()
    for number, row in enumerate(positions, 1):
        secid = str(row.get("secid") or "").strip().upper()
        quantity = row.get("quantity")
        value = row.get("market_value")
        try:
            q = float(quantity)
            v = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"Строка {number}: укажите количество и стоимость позиции") from None
        if (not secid or not math.isfinite(q) or q <= 0 or not q.is_integer()
                or not math.isfinite(v) or v <= 0):
            raise ValueError(f"Строка {number}: заполните SECID, целое количество и стоимость больше нуля")
        if secid in seen:
            raise ValueError(f"Позиция {secid} повторяется. Объедините её в одну строку")
        seen.add(secid)
        result.append({"secid": secid, "quantity": int(q), "market_value": v})
    return {"cash": cash, "positions": result}


def load_portfolio(path: Path):
    if not path.exists():
        return {"cash": 50_000.0, "positions": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    return validate_portfolio(data["cash"], data["positions"])


def save_portfolio(path: Path, snapshot):
    snapshot = validate_portfolio(snapshot["cash"], snapshot["positions"])
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write a temporary file beside the destination so replacement is atomic.
    fd, tmp = tempfile.mkstemp(prefix=".portfolio-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(snapshot, out, ensure_ascii=False, indent=2, allow_nan=False)
            out.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
