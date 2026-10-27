import json
from pathlib import Path

import pytest

from bond_strategy.ingestion.moex import IssClient, IssError, block_to_records, to_float

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_block_to_records_maps_columns():
    payload = load_fixture("iss_board_sample.json")
    rows = block_to_records(payload, "securities")
    assert rows[0]["SECID"] == "RU000A10DS74"
    assert rows[0]["MATDATE"] == "2028-06-03"
    assert len(rows) == 2


def test_missing_block_raises_not_empty():
    payload = load_fixture("iss_board_sample.json")
    with pytest.raises(IssError):
        block_to_records(payload, "coupons")


def test_row_length_mismatch_raises():
    bad = {"securities": {"columns": ["A", "B"], "data": [[1]]}}
    with pytest.raises(IssError):
        block_to_records(bad, "securities")


def test_null_marketdata_stays_none_not_zero():
    payload = load_fixture("iss_board_sample.json")
    rows = block_to_records(payload, "marketdata")
    illiquid = rows[1]
    assert illiquid["BID"] is None
    assert to_float(illiquid["BID"]) is None
    assert to_float(illiquid["VALTODAY"]) == 0.0  # реально нулевой оборот, а не пропуск


def test_to_float_handles_strings_and_garbage():
    assert to_float("15.19") == 15.19
    assert to_float("") is None
    assert to_float("n/a") is None
    assert to_float(None) is None


def test_client_saves_raw_and_hash(tmp_path):
    body = json.dumps(load_fixture("iss_board_sample.json")).encode()
    client = IssClient(tmp_path, fetch=lambda url, t: body, sleep=lambda s: None)
    payload, snap = client.get_json("/engines/stock/markets/bonds/boards/TQCB/securities.json")
    assert snap.file.exists()
    assert snap.file.read_bytes() == body
    assert len(snap.sha256) == 64
    assert payload["securities"]["data"][0][0] == "RU000A10DS74"


def test_client_retries_then_fails_loudly(tmp_path):
    calls = {"n": 0}

    def broken(url, timeout):
        calls["n"] += 1
        raise OSError("network down")

    client = IssClient(tmp_path, fetch=broken, retries=3, sleep=lambda s: None)
    with pytest.raises(IssError):
        client.get_json("/engines/stock/markets/bonds/securities.json")
    assert calls["n"] == 3


def test_client_recovers_after_transient_error(tmp_path):
    body = b'{"securities": {"columns": [], "data": []}}'
    state = {"n": 0}

    def flaky(url, timeout):
        state["n"] += 1
        if state["n"] == 1:
            raise OSError("timeout")
        return body

    client = IssClient(tmp_path, fetch=flaky, retries=3, sleep=lambda s: None)
    payload, _ = client.get_json("/x.json")
    assert state["n"] == 2
    assert "securities" in payload


def test_invalid_json_is_not_saved_as_success(tmp_path):
    client = IssClient(tmp_path, fetch=lambda u, t: b"<html>502</html>", retries=2, sleep=lambda s: None)
    with pytest.raises(IssError):
        client.get_json("/x.json")
