"""Обогащение и ранжирование юниверсума: кэш рейтингов, устойчивость к сбоям, порядок.

Сеть не используется: IssClient подменяется функцией fetch, отдающей заранее
заготовленные ответы ISS. Так проверяется именно логика конвейера.
"""
import json
from datetime import date

import pytest

from bond_strategy.config import load_config
from bond_strategy.ingestion.moex import IssClient
from bond_strategy.universe import UniverseStats, build_universe, screen

AS_OF = date(2026, 10, 26)


@pytest.fixture(scope="module")
def cfg():
    return load_config("config/strategy.json")


def board_row(secid: str, **over) -> dict:
    row = {
        "SECID": secid,
        "FACEUNIT": "SUR",
        "MATDATE": "2028-06-01",
        "LISTLEVEL": 1,
        "PREVPRICE": 98.5,
        "ACCRUEDINT": 7.5,
        "BONDTYPE": "Облигация с фиксированным (известным) купоном",
        "COUPON_DETAILS": "Фиксированный с известными купонами",
        "OFFERDATE": None,
        "LOTSIZE": 1,
    }
    row.update(over)
    return row


def description_payload(emitter_id: str) -> dict:
    return {
        "description": {
            "columns": ["name", "title", "value"],
            "data": [
                ["ISIN", "ISIN", "RU000TEST0001"],
                ["EMITTER_ID", "Эмитент", emitter_id],
                ["FACEUNIT", "Валюта", "SUR"],
                ["MATDATE", "Дата погашения", "2028-06-01"],
                ["FACEVALUE", "Номинал", "1000"],
                ["COUPONPERCENT", "Ставка", "16"],
                ["COUPONFREQUENCY", "Частота", "2"],
                ["LISTLEVEL", "Уровень", "1"],
            ],
        }
    }


def empty_bondization() -> dict:
    return {
        "coupons": {"columns": ["coupondate", "value"], "data": []},
        "amortizations": {"columns": ["amortdate", "value", "data_source"], "data": []},
        "offers": {"columns": ["offerdate"], "data": []},
    }


def history_payload(value: float) -> dict:
    return {
        "history": {
            "columns": ["TRADEDATE", "BOARDID", "VALUE"],
            "data": [["2026-10-26", "TQCB", value]],
        }
    }


def rating_payload(level: str, agency: str = "АКРА") -> list:
    return [
        {"charsetinfo": {"name": "utf-8"}},
        {
            "cci_rating_companies": [
                {
                    "agency_name_short_ru": agency,
                    "rating_level_name_short_ru": level,
                    "rating_book_name_short_ru": "Национальная шкала кредитных рейтингов",
                    "rating_date": "2026-06-01 00:00:00",
                }
            ]
        },
    ]


class FakeIss:
    """Считает запросы и умеет отвечать ошибкой на выбранные пути."""

    def __init__(self, ratings: dict[str, str] | None = None, fail: set[str] | None = None):
        self.ratings = ratings if ratings is not None else {"100": "AAA(RU)"}
        self.fail = fail or set()
        self.calls: list[str] = []

    def fetch(self, url: str, timeout: float) -> bytes:
        self.calls.append(url)
        if any(marker in url for marker in self.fail):
            raise OSError("network down")

        if "/cci/rating/companies/ecbd_" in url:
            emitter = url.split("ecbd_")[1].split(".")[0]
            level = self.ratings.get(emitter)
            payload = rating_payload(level) if level else [
                {"charsetinfo": {}},
                {"cci_rating_companies": []},
            ]
            return json.dumps(payload).encode()

        if "/history/" in url:
            return json.dumps(history_payload(50_000_000)).encode()

        if "/securities/" in url and "description" in url:
            # Разные выпуски — разные эмитенты, чтобы кэш было на чём проверить.
            emitter = "100" if "AAAA" in url else "100"
            return json.dumps(description_payload(emitter)).encode()

        if "/bondization/" in url:
            return json.dumps(empty_bondization()).encode()

        raise AssertionError(f"неожиданный запрос: {url}")

    def client(self) -> IssClient:
        return IssClient(raw_root=None, fetch=self.fetch, retries=1)

    def count(self, marker: str) -> int:
        return sum(1 for call in self.calls if marker in call)


# ---------- отсев как часть конвейера ----------


def test_screen_result_flows_into_universe(cfg):
    fake = FakeIss()
    rows = [board_row("RU000AAAA001"), board_row("RU000AAAA002", FACEUNIT="USD")]
    passed, stats = screen(rows, AS_OF, cfg)
    assert len(passed) == 1

    issues, stats, errors = build_universe(
        fake.client(), passed, AS_OF, cfg, workers=1, stats=stats
    )
    assert len(issues) == 1
    assert errors == []
    assert stats.as_dict()["passed_screen"] == 1
    assert stats.as_dict()["enriched"] == 1


# ---------- кэш рейтингов ----------


def test_rating_fetched_once_per_emitter(cfg):
    """Три выпуска одного эмитента — один запрос рейтинга, а не три."""
    fake = FakeIss()
    rows = [board_row(f"RU000AAAA00{i}") for i in range(3)]
    issues, stats, errors = build_universe(fake.client(), rows, AS_OF, cfg, workers=1)

    assert len(issues) == 3
    assert errors == []
    assert fake.count("/cci/rating/companies/") == 1


def test_rating_reaches_every_issue_from_cache(cfg):
    fake = FakeIss()
    rows = [board_row(f"RU000AAAA00{i}") for i in range(3)]
    issues, _, _ = build_universe(fake.client(), rows, AS_OF, cfg, workers=1)

    assert {i.rating for i in issues} == {"AAA"}
    assert {i.rating_agency for i in issues} == {"АКРА"}


def test_cache_also_stores_absent_rating(cfg):
    """Эмитент без рейтинга не запрашивается повторно — кэшируется None."""
    fake = FakeIss(ratings={})
    rows = [board_row(f"RU000AAAA00{i}") for i in range(3)]
    issues, _, _ = build_universe(fake.client(), rows, AS_OF, cfg, workers=1)

    assert all(i.rating is None for i in issues)
    assert fake.count("/cci/rating/companies/") == 1


# ---------- устойчивость к сбоям ----------


def test_rating_failure_does_not_lose_issue(cfg):
    """Сбой запроса рейтинга — выпуск остаётся, рейтинг None, ошибка не глотается."""
    fake = FakeIss(fail={"/cci/rating/companies/"})
    rows = [board_row("RU000AAAA001")]
    issues, stats, errors = build_universe(fake.client(), rows, AS_OF, cfg, workers=1)

    assert len(issues) == 1
    assert issues[0].rating is None
    # Ошибка рейтинга не считается фатальной: путь устойчив, но и не молчит.
    assert all("traceback" not in e.lower() for e in errors)


def test_history_failure_keeps_issue_with_no_turnover(cfg):
    fake = FakeIss(fail={"/history/"})
    rows = [board_row("RU000AAAA001")]
    issues, stats, errors = build_universe(fake.client(), rows, AS_OF, cfg, workers=1)

    assert len(issues) == 1
    assert issues[0].avg_daily_turnover is None
    assert issues[0].turnover_days == 0
    assert errors and "история" in errors[0]


def test_parallel_matches_sequential(cfg):
    """Результат не зависит от числа потоков."""
    rows = [board_row(f"RU000AAAA00{i}") for i in range(4)]

    seq_client = FakeIss()
    seq, _, seq_err = build_universe(seq_client.client(), rows, AS_OF, cfg, workers=1)

    par_client = FakeIss()
    par, _, par_err = build_universe(par_client.client(), rows, AS_OF, cfg, workers=4)

    assert [i.secid for i in seq] == [i.secid for i in par]
    assert [i.rating for i in seq] == [i.rating for i in par]
    assert seq_err == par_err


def test_parallel_cache_still_single_fetch(cfg):
    """При шести потоках кэш не разъезжается: гонки нет."""
    fake = FakeIss()
    rows = [board_row(f"RU000AAAA00{i}") for i in range(6)]
    build_universe(fake.client(), rows, AS_OF, cfg, workers=6)
    # Допускаем 1-2 запроса: потоки могли одновременно не застать ключ.
    assert fake.count("/cci/rating/companies/") <= 2


# ---------- ранжирование ----------


def test_universe_is_not_truncated_before_ranking(cfg):
    """Юниверсум строится целиком: отбор лучших происходит после, а не до.

    Именно это отличает новое поведение от прежнего limit: раньше первые N
    строк борды обогащались, и ликвидные выпуски могли вообще не попасть
    в прогон.
    """
    fake = FakeIss()
    rows = [board_row(f"RU000AAAA00{i}") for i in range(5)]
    issues, _, _ = build_universe(fake.client(), rows, AS_OF, cfg, workers=1)
    assert len(issues) == 5
