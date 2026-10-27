"""Минимальный HTTP-клиент MOEX ISS с сохранением сырых ответов.

Правила:
- ошибка загрузки поднимается исключением, а не превращается в пустой результат;
- каждый ответ сохраняется отдельно вместе с SHA-256 и временем загрузки;
- парсинг не подставляет нули и предыдущие значения вместо отсутствующих.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

BASE_URL = "https://iss.moex.com/iss"


class IssError(RuntimeError):
    """Ошибка загрузки или структуры ответа ISS."""


@dataclass(frozen=True)
class RawSnapshot:
    source: str
    path: str
    sha256: str
    collected_at: str
    file: Path


def _default_fetch(url: str, timeout: float) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read()


class IssClient:
    def __init__(
        self,
        raw_root: str | Path,
        fetch: Callable[[str, float], bytes] = _default_fetch,
        retries: int = 3,
        timeout: float = 20.0,
        backoff: float = 1.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.raw_root = Path(raw_root)
        self._fetch = fetch
        self.retries = retries
        self.timeout = timeout
        self.backoff = backoff
        self._sleep = sleep

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> tuple[dict, RawSnapshot]:
        query = {"iss.meta": "off"}
        query.update(params or {})
        url = f"{BASE_URL}{path}?{urllib.parse.urlencode(query)}"

        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                body = self._fetch(url, self.timeout)
                payload = json.loads(body)
                snapshot = self._save_raw(path, body)
                return payload, snapshot
            except (OSError, ValueError) as exc:
                last_error = exc
                if attempt < self.retries:
                    self._sleep(self.backoff ** attempt)
        raise IssError(f"ISS недоступен после {self.retries} попыток: {path}: {last_error}")

    def _save_raw(self, path: str, body: bytes) -> RawSnapshot:
        now = datetime.now(timezone.utc)
        day = now.strftime("%Y-%m-%d")
        safe = path.strip("/").replace("/", "__").replace(".json", "")
        target_dir = self.raw_root / "moex_iss" / day
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = now.strftime("%H%M%S%f")
        file = target_dir / f"{safe}_{stamp}.json"
        file.write_bytes(body)
        digest = hashlib.sha256(body).hexdigest()
        return RawSnapshot(
            source="moex_iss",
            path=path,
            sha256=digest,
            collected_at=now.isoformat(),
            file=file,
        )


def block_to_records(payload: dict, block: str) -> list[dict]:
    """Преобразует блок ISS {columns, data} в список словарей.

    Если блока нет или структура неверна — IssError, без молчаливой пустоты.
    """
    if block not in payload:
        raise IssError(f"В ответе нет блока '{block}'")
    data = payload[block]
    columns = data.get("columns")
    rows = data.get("data")
    if not isinstance(columns, list) or not isinstance(rows, list):
        raise IssError(f"Блок '{block}' имеет неверную структуру")
    for row in rows:
        if len(row) != len(columns):
            raise IssError(f"Строка блока '{block}' не совпадает с числом колонок")
    return [dict(zip(columns, row)) for row in rows]


def to_float(value: Any) -> float | None:
    """ISS отдаёт числа строками или null. None означает «нет данных»."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
