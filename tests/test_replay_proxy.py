import httpx
import pytest
from conftest import chat_req, chat_resp
from fastapi.testclient import TestClient

from agentreplay.proxy import create_app
from agentreplay.replay import Replayer, ReplayMiss
from agentreplay.schema import Exchange


def _mock_upstream(counter):
    def handler(request: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        return httpx.Response(200, json=chat_resp(f"answer-{counter['n']}"))

    return httpx.MockTransport(handler)


def test_record_then_replay_offline(store):
    calls = {"n": 0}
    rec = TestClient(
        create_app(store, "record", trace_name="t", transport=_mock_upstream(calls))
    )
    r1 = rec.post("/v1/chat/completions", json=chat_req("q1")).json()
    r2 = rec.post("/v1/chat/completions", json=chat_req("q2")).json()
    trace_id = rec.app.state.trace_id
    assert calls["n"] == 2 and len(store.exchanges(trace_id)) == 2

    # Replay never touches upstream (no transport, unreachable URL).
    rep = TestClient(
        create_app(
            store, "replay", trace_id=trace_id, upstream_url="http://127.0.0.1:1"
        )
    )
    assert (
        rep.post("/v1/chat/completions", json=chat_req("q2")).json() == r2
    )  # order-independent by hash
    assert rep.post("/v1/chat/completions", json=chat_req("q1")).json() == r1
    assert calls["n"] == 2


def test_strict_miss_returns_409_lenient_tolerates_param_drift(store):
    calls = {"n": 0}
    rec = TestClient(create_app(store, "record", transport=_mock_upstream(calls)))
    rec.post("/v1/chat/completions", json=chat_req("q", temperature=0.0))
    tid = rec.app.state.trace_id

    strict = TestClient(create_app(store, "replay", trace_id=tid, replay_mode="strict"))
    assert (
        strict.post(
            "/v1/chat/completions", json=chat_req("q", temperature=0.9)
        ).status_code
        == 409
    )

    lenient = TestClient(
        create_app(store, "replay", trace_id=tid, replay_mode="lenient")
    )
    assert (
        lenient.post(
            "/v1/chat/completions", json=chat_req("q", temperature=0.9)
        ).status_code
        == 200
    )


def test_repeated_identical_requests_replay_in_order():
    exs = [
        Exchange.from_request(
            "t", i, chat_req("loop"), status_code=200, response=chat_resp(f"r{i}")
        )
        for i in range(3)
    ]
    rp = Replayer(exs)
    texts = [
        rp.next(chat_req("loop")).response["choices"][0]["message"]["content"]
        for _ in range(3)
    ]
    assert texts == ["r0", "r1", "r2"]
    with pytest.raises(ReplayMiss):
        rp.next(
            chat_req("loop")
        )  # a 4th identical call = agent looped more than recorded


def test_upstream_failure_is_recorded(store):
    def boom(request):
        raise httpx.ConnectTimeout("timed out")

    rec = TestClient(create_app(store, "record", transport=httpx.MockTransport(boom)))
    assert rec.post("/v1/chat/completions", json=chat_req()).status_code == 502
    ex = store.exchanges(rec.app.state.trace_id)[0]
    assert ex.status_code == 599 and "ConnectTimeout" in ex.error
