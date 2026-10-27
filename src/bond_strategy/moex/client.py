"""HTTP-клиент MOEX ISS с кэшированием снимков.

Каждый ответ сохраняется в data/raw/moex/<дата>/ как gzip-JSON. Это даёт
два свойства, без которых аналитическое ядро не воспроизводимо:

1. повторный расчёт не зависит от доступности ISS;
2. можно доказать, какие именно данные были получены на дату решения.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

ISS_BASE = "https://iss.moex.com/iss"
RAW_ROOT = Path("data/raw/moex")


class IssError(RuntimeError):
    """Ошибка обращения к ISS. Не маскируется пустым результатом."""


@dataclass
class IssClient:
    base: str = ISS_BASE
    timeout: float = 30.0
    retries: int = 3
    min_interval: float = 0.05
    raw_root: Path = RAW_ROOT
    offline: bool = False
    calls: int = 0
    cache_hits: int = 0
    _last_call: float = field(default=0.0, repr=False)

    # ---------- кэш ----------

    def _cache_path(self, path: str, params: dict[str, str], stamp: str) -> Path:
        readable = re.sub(r"[^A-Za-z0-9]+", "_", path).strip("_")[:60]
        digest = hashlib.sha256(
            (path + "?" + urllib.parse.urlencode(sorted(params.items()))).encode()
        ).hexdigest()[:10]
        return self.raw_root / stamp / f"{readable}_{digest}.json.gz"

    def _read_cache(self, file: Path) -> dict | None:
        if not file.exists():
            return None
        try:
            with gzip.open(file, "rt", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None

    def _write_cache(self, file: Path, payload: dict) -> None:
        file.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(file, "wt", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)

    # ---------- запрос ----------

    def get(self, path: str, stamp: str | None = None, **params: object) -> dict:
        """GET с ретраями. При offline=True читает только из кэша."""
        clean = {k: str(v) for k, v in params.items() if v is not None}
        clean.setdefault("iss.meta", "off")
        stamp = stamp or date.today().isoformat()
        cache_file = self._cache_path(path, clean, stamp)

        cached = self._read_cache(cache_file)
        if cached is not None:
            self.cache_hits += 1
            return cached
        if self.offline:
            raise IssError(f"Нет снимка в кэше: {cache_file}")

        url = f"{self.base}{path}?{urllib.parse.urlencode(clean)}"
        last_error: Exception | None = None

        for attempt in range(self.retries):
            self._throttle()
            try:
                with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read())
                self.calls += 1
                self._write_cache(cache_file, payload)
                return payload
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code in (400, 403, 404):
                    raise IssError(f"ISS {exc.code} для {path}: {exc.reason}") from exc
                time.sleep(0.5 * (attempt + 1))
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                time.sleep(0.5 * (attempt + 1))

        raise IssError(f"ISS недоступен для {path}: {last_error}")

    def _throttle(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()


def block(payload: dict, name: str) -> list[dict]:
    """Блок ISS в список словарей. Отсутствующий блок — пустой список."""
    data = payload.get(name)
    if not data or not data.get("columns"):
        return []
    cols = data["columns"]
    return [dict(zip(cols, row)) for row in data.get("data", [])]


def num(value: object) -> float | None:
    """Число или None. Пустая строка и 0.0 в ISS не всегда значат одно и то же."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def dt(value: object) -> date | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None
