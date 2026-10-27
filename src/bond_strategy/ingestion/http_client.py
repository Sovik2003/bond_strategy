"""HTTP-клиент MOEX ISS: повторные попытки, пауза между запросами, сырые снимки.

Сырой ответ сохраняется до нормализации: при ошибке разбора не нужно
повторно обращаться к бирже, а решение можно воспроизвести.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class FetchError(Exception):
    """Сетевая ошибка или невалидный ответ ISS."""


@dataclass
class HttpResult:
    url: str
    payload: dict[str, Any]
    fetched_at: datetime
    raw_path: Path | None
    attempts: int


def parse_block(payload: dict[str, Any], name: str) -> list[dict[str, Any]]:
    """Превращает блок ISS {'columns': [...], 'data': [[...]]} в список словарей."""
    block = payload.get(name)
    if not block or not block.get("columns"):
        return []
    columns = block["columns"]
    return [dict(zip(columns, row)) for row in block.get("data") or []]


class IssClient:
    BASE = "https://iss.moex.com/iss"

    def __init__(
        self,
        raw_dir: str | Path = "data/raw/moex",
        user_agent: str = "bond-strategy/0.1 (analytical research)",
        timeout: float = 25.0,
        max_attempts: int = 3,
        backoff: float = 1.5,
        min_interval: float = 0.25,
    ) -> None:
        self.raw_dir = Path(raw_dir)
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.backoff = backoff
        self.min_interval = min_interval
        self._last_request_at = 0.0

    def _build_url(self, path: str, params: dict[str, Any] | None) -> str:
        base = path if path.startswith("http") else f"{self.BASE}{path}"
        if not params:
            return base
        clean = {k: v for k, v in params.items() if v is not None}
        return f"{base}?{urllib.parse.urlencode(clean)}"

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_request_at = time.monotonic()

    def _save_raw(self, key: str, body: bytes, fetched_at: datetime) -> Path | None:
        """Сохраняет сырой ответ. Отсутствие прав не должно ломать загрузку."""
        safe = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        try:
            folder = self.raw_dir / fetched_at.strftime("%Y-%m-%d")
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{safe}.json"
            path.write_bytes(body)
            (folder / f"{safe}.meta.json").write_text(
                json.dumps(
                    {"key": key, "fetched_at": fetched_at.isoformat(), "bytes": len(body)},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            return path
        except OSError:
            return None

    def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        snapshot_key: str | None = None,
        save: bool = True,
    ) -> HttpResult:
        url = self._build_url(path, params)
        last_error: Exception | None = None

        for attempt in range(1, self.max_attempts + 1):
            self._throttle()
            request = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = response.read()
                payload = json.loads(body)
                fetched_at = datetime.now(timezone.utc)
                raw_path = (
                    self._save_raw(snapshot_key or url, body, fetched_at) if save else None
                )
                return HttpResult(url, payload, fetched_at, raw_path, attempt)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
            except json.JSONDecodeError as exc:
                # ответ получен, но не разобрался: повтор не поможет
                raise FetchError(f"{url}: некорректный JSON ({exc})") from exc
            if attempt < self.max_attempts:
                time.sleep(self.backoff * attempt)

        raise FetchError(f"{url}: {type(last_error).__name__}: {last_error}")
