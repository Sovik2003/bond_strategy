"""Дисковый кэш обогащённых выпусков.

Зачем. Обогащение выпуска — это четыре сетевых запроса (описание,
график выплат, рейтинг эмитента, история ликвидности). На 325 выпусках
это больше тысячи запросов, и один обрыв связи обнулял бы всю работу.
Здесь каждый обработанный выпуск сразу сохраняется на диск, а следующий
запуск начинается с того места, где остановился предыдущий.

Ключ кэша — SECID плюс отпечаток входных условий: дата расчёта и
config_id. Без отпечатка кэш от прошлой даты отдавал бы устаревшие цены
и сроки, и отчёт считался бы по вчерашнему рынку, не сообщая об этом.
Поэтому смена даты или конфига просто делает старый кэш невидимым, а не
требует ручной очистки.

Что НЕ кэшируется: сама доска (`fetch_board`) — она приходит одним
запросом и обновляется целиком.
"""
from __future__ import annotations

import dataclasses
import json
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Any

from ..models import AmortizationPayment, BondIssue, DataStatus, Quality


def _encode(value: Any) -> Any:
    """Значение -> JSON-совместимый вид.

    Даты пишутся ISO-строкой, перечисления — своим значением, вложенные
    dataclass'ы (амортизация) — словарём. Рекурсия нужна потому, что модель
    выпуска содержит всё это одновременно.
    """
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _encode(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {str(k): _encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(v) for v in value]
    return value


def issue_to_dict(issue: BondIssue) -> dict:
    """Выпуск -> словарь для кэша."""
    return {f.name: _encode(getattr(issue, f.name)) for f in dataclasses.fields(issue)}


def issue_from_dict(data: dict) -> BondIssue:
    """Словарь -> выпуск.

    Обратное преобразование явное, а не через `BondIssue(**data)`: даты и
    перечисления пришли строками, и молчаливое доверие к ним дало бы объект,
    который падает позже, в расчёте доходности.
    """
    params = dict(data)
    for key in ("maturity_date", "offer_date", "price_date", "rating_date"):
        raw = params.get(key)
        params[key] = date.fromisoformat(raw) if raw else None

    params["amortization"] = [
        AmortizationPayment(date.fromisoformat(item["payment_date"]), float(item["share_of_nominal"]))
        for item in params.get("amortization") or []
    ]
    params["coupon_schedule"] = [
        (date.fromisoformat(item[0]), float(item[1])) for item in params.get("coupon_schedule") or []
    ]
    params["status"] = {key: DataStatus(value) for key, value in (params.get("status") or {}).items()}
    params["quality"] = Quality(params.get("quality") or Quality.HIGH.value)
    return BondIssue(**params)


class MarketCache:
    """Кэш обогащённых выпусков, привязанный к отпечатку условий.

    Один файл на отпечаток. Формат — словарь `{secid: {...}}`, обновляемый
    после каждого выпуска: запись целиком после каждого обогащения была бы
    квадратичной по объёму, но здесь 325 записей, и простота важнее.
    """

    def __init__(self, root: str | Path, fingerprint: str) -> None:
        self.root = Path(root)
        self.fingerprint = fingerprint
        self.path = self.root / f"market-{fingerprint}.json"
        self._issues: dict[str, dict] = {}
        self._load()

    @staticmethod
    def make_fingerprint(as_of: date, config_id: str) -> str:
        """Отпечаток условий: дата расчёта и конфигурация."""
        return f"{as_of.isoformat()}-{config_id}"

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            self._issues = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Битый файл не должен останавливать прогон: считаем кэш пустым и
            # перезаписываем его заново.
            self._issues = {}

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self._issues, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        tmp.replace(self.path)

    def has(self, secid: str) -> bool:
        return secid in self._issues

    def get(self, secid: str) -> BondIssue | None:
        data = self._issues.get(secid)
        if data is None:
            return None
        try:
            return issue_from_dict(data)
        except (KeyError, TypeError, ValueError):
            # Выпуск, сохранённый старой версией модели, лучше перезагрузить,
            # чем падать на разборе.
            self._issues.pop(secid, None)
            return None

    def put(self, issue: BondIssue) -> None:
        self._issues[issue.secid] = issue_to_dict(issue)

    def put_many(self, issues: list[BondIssue]) -> None:
        for issue in issues:
            self.put(issue)

    def all_issues(self) -> list[BondIssue]:
        out = []
        for secid in sorted(self._issues):
            issue = self.get(secid)
            if issue is not None:
                out.append(issue)
        return out

    def __len__(self) -> int:
        return len(self._issues)
