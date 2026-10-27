"""MOEX ISS: HTTP-клиент и разбор ответов.

Правила, без которых расчёт врёт:
- ошибка загрузки поднимается исключением, а не превращается в пустой список;
- сырой ответ сохраняется до нормализации вместе с SHA-256;
- отсутствующее значение остаётся None, а не нулём;
- bondization отдаёт данные страницами по 20 строк. Проверено на выпуске
  RU000A10FEP7: всего 106 строк, из них 21 с ненулевым платежом, и одна
  ненулевая выплата (120.02 из 1000) лежит за пределами первой страницы.
  Поэтому блок дочитывается до конца, а не обрезается на 20 строках;
- актуальная амортизация — это строки с data_source != 'maturity';
  строка 'maturity' и есть погашение, её нельзя считать дважды.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

BASE_URL = "https://iss.moex.com/iss"

BOARD_CORP = "TQCB"
BOARD_OFZ = "TQOB"
PAGE_SIZE = 20

# BOND_TYPE в ISS описывает способ погашения, а не тип купона.
# Тип купона приходится определять по COUPON_BENCHMARK/COUPON_DETAILS.
BOND_TYPE_FIXED_COUPON = {
    "Облигация с фиксированным (известным) купоном",
    "Облигация с известным купоном",
    "Облигация дисконтная",
}
BOND_TYPE_FLOATING_COUPON = {
    "Облигация с плавающим купоном",
    "Облигация с переменным купоном",
}
BOND_TYPE_AMORTIZING = {"Амортизируемая облигация"}
BOND_TYPE_FOREIGN = {"Валютная облигация"}
BOND_TYPE_STRUCTURED = {"Облигация структурная", "Облигация с ипотечным покрытием"}


class IssError(RuntimeError):
    """Ошибка загрузки или структуры ответа ISS."""


@dataclass(frozen=True)
class RawSnapshot:
    path: str
    sha256: str
    collected_at: str
    file: Path


def _http_fetch(url: str, timeout: float) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read()


class IssClient:
    def __init__(
        self,
        raw_root: str | Path | None = None,
        fetch: Callable[[str, float], bytes] = _http_fetch,
        timeout: float = 25.0,
        retries: int = 3,
        backoff: float = 1.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.raw_root = Path(raw_root) if raw_root else None
        self._fetch = fetch
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self._sleep = sleep

    def get(self, path: str, **params: Any) -> tuple[dict, RawSnapshot | None]:
        query: dict[str, Any] = {"iss.meta": "off"}
        query.update({k: v for k, v in params.items() if v is not None})
        url = f"{BASE_URL}{path}?{urllib.parse.urlencode(query)}"

        last: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                body = self._fetch(url, self.timeout)
                payload = json.loads(body)
            except (OSError, ValueError) as exc:
                last = exc
                if attempt < self.retries:
                    self._sleep(self.backoff ** attempt)
                continue
            return payload, self._save(path, body)
        raise IssError(f"ISS недоступен: {path}: {last}")

    def _save(self, path: str, body: bytes) -> RawSnapshot | None:
        if self.raw_root is None:
            return None
        now = datetime.now(timezone.utc)
        folder = self.raw_root / now.strftime("%Y-%m-%d")
        folder.mkdir(parents=True, exist_ok=True)
        safe = path.strip("/").replace("/", "__").replace(".json", "")
        file = folder / f"{safe}_{now.strftime('%H%M%S%f')}.json"
        file.write_bytes(body)
        return RawSnapshot(
            path=path,
            sha256=hashlib.sha256(body).hexdigest(),
            collected_at=now.isoformat(),
            file=file,
        )


def block(payload: dict, name: str) -> list[dict]:
    """Блок {columns, data} -> список словарей. Нет блока — ошибка."""
    if name not in payload:
        raise IssError(f"В ответе нет блока '{name}'")
    data = payload[name]
    columns, rows = data.get("columns"), data.get("data")
    if not isinstance(columns, list) or not isinstance(rows, list):
        raise IssError(f"Блок '{name}' имеет неверную структуру")
    for row in rows:
        if len(row) != len(columns):
            raise IssError(f"Строка блока '{name}' не совпадает по длине с колонками")
    return [dict(zip(columns, row)) for row in rows]


def num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def dt(value: Any) -> date | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def coupon_type_from(
    bond_type: str | None, benchmark: str | None, details: str | None
) -> str:
    """Тип купона. BOND_TYPE в ISS говорит о погашении, а не о купоне.

    Плавающий купон определяется по COUPON_BENCHMARK: у флоатеров там
    стоит ссылка на базовую ставку (например RREFKEYR).
    """
    if bond_type in BOND_TYPE_FOREIGN:
        return "FOREIGN"
    if bond_type in BOND_TYPE_STRUCTURED:
        return "STRUCTURED"
    if benchmark:
        return "FLOATING"
    if bond_type in BOND_TYPE_FLOATING_COUPON:
        return "FLOATING"
    if bond_type in BOND_TYPE_AMORTIZING or bond_type in BOND_TYPE_FIXED_COUPON:
        return "FIXED"
    if bond_type and "купон" in bond_type.lower():
        if "плавающ" in bond_type.lower() or "переменн" in bond_type.lower():
            return "FLOATING"
        return "FIXED"
    return "UNKNOWN"


def fetch_board(client: IssClient, board: str = BOARD_CORP) -> list[dict]:
    """Выпуски режима с рыночными данными, объединённые по SECID."""
    payload, _ = client.get(
        f"/engines/stock/markets/bonds/boards/{board}/securities.json",
        **{"iss.only": "securities,marketdata"},
    )
    securities = block(payload, "securities")
    market = {r["SECID"]: r for r in block(payload, "marketdata")}
    merged = []
    for sec in securities:
        row = dict(sec)
        row.update(market.get(sec["SECID"], {}))
        merged.append(row)
    return merged


def fetch_description(client: IssClient, secid: str) -> dict[str, str]:
    payload, _ = client.get(f"/securities/{secid}.json", **{"iss.only": "description"})
    return {row[0]: row[2] for row in payload["description"]["data"]}


def fetch_bondization(client: IssClient, secid: str) -> dict[str, list[dict]]:
    """Купоны, амортизация, оферты с полной пагинацией.

    ISS отдаёт по 20 строк на страницу. Без цикла по start теряется
    большая часть графика — это проверено на реальном выпуске.
    """
    result: dict[str, list[dict]] = {"coupons": [], "amortizations": [], "offers": []}
    for name in result:
        start = 0
        while True:
            payload, _ = client.get(
                f"/statistics/engines/stock/markets/bonds/bondization/{secid}.json",
                **{"iss.only": name, "start": start},
            )
            if name not in payload or not payload[name].get("columns"):
                break
            rows = block(payload, name)
            if not rows:
                break
            result[name].extend(rows)
            start += len(rows)
            if len(rows) < PAGE_SIZE:
                break
    return result
