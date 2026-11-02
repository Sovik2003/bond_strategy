"""Validation and local persistence of manually entered portfolio holdings."""
from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path


def validate_portfolio(cash, positions):
    """Return a normalized snapshot; each value is total RUB including accrued interest.

    Три способа убрать проданную позицию, и все три должны работать, потому что
    пользователь пробует их в произвольном порядке:

        отметить колонку «Продано»   — явное действие, попадает в отчёт;
        поставить количество 0        — естественное «больше не держу»;
        оставить строку пустой        — так выглядит незаполненная строка от «+».

    Раньше все три давали ошибку «заполните количество и стоимость больше нуля»,
    и портфель не сохранялся вовсе. Хуже: ошибка выглядела как требование
    заполнить поля, поэтому выход казался один — вписать выдуманное количество.
    """
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
        # Явная отметка «продано» в форме.
        if row.get("remove") is True:
            continue

        # SECID нормализуется с оглядкой на NaN: `str(float("nan"))` даёт строку
        # "nan", и она выглядит как заполненное поле. Пустая строка от «+»
        # приходила как NaN именно так и не опознавалась.
        raw_secid = row.get("secid")
        secid = "" if _blank(raw_secid) else str(raw_secid).strip().upper()
        quantity = row.get("quantity")
        value = row.get("market_value")

        # Пустая строка от кнопки «+». Пропускаем молча: пользователь добавил
        # строку и передумал, это не ошибка, требующая его внимания.
        if not secid and _blank(quantity) and _blank(value):
            continue

        try:
            q = float(quantity)
            v = float(value)
        except (TypeError, ValueError):
            raise ValueError(
                f"Строка {number}: укажите количество и стоимость позиции"
            ) from None

        # Количество 0 — «продал всё». Убираем позицию, но не молча: разницу
        # между «удалил» и «опечатался» видно по отчёту о сохранении.
        if secid and math.isfinite(q) and q == 0:
            seen.add(secid)
            continue

        if (not secid or not math.isfinite(q) or q < 0 or not q.is_integer()
                or not math.isfinite(v) or v <= 0):
            raise ValueError(
                f"Строка {number}: заполните SECID, целое количество "
                f"и стоимость больше нуля. Чтобы убрать проданную позицию, "
                f"поставьте количество 0."
            )
        if secid in seen:
            raise ValueError(f"Позиция {secid} повторяется. Объедините её в одну строку")
        seen.add(secid)
        result.append({"secid": secid, "quantity": int(q), "market_value": v})
    return {"cash": cash, "positions": result}


def _blank(value) -> bool:
    """Пустая ячейка: None, NaN или пустая строка — так data_editor отдаёт пустоту.

    Строка "nan" тоже считается пустой. pandas превращает NaN в текстовой
    колонке в строку, и без этой проверки пустая строка от «+» становилась
    позицией с SECID «NAN» и нулевой стоимостью.
    """
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    text = str(value).strip()
    return text == "" or text.lower() == "nan"


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
