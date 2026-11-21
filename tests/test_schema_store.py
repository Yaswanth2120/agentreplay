from conftest import chat_req, chat_resp

from agentreplay.schema import (
    LENIENT_KEYS,
    STRICT_KEYS,
    Exchange,
    Trace,
    canonical_hash,
)


def test_hash_ignores_key_order_and_irrelevant_fields():
    a = {"model": "m", "messages": [{"role": "user", "content": "x"}], "user": "u1"}
    b = {"user": "u2", "messages": [{"role": "user", "content": "x"}], "model": "m"}
    assert canonical_hash(a, STRICT_KEYS) == canonical_hash(b, STRICT_KEYS)


def test_lenient_ignores_temperature_strict_does_not():
    a, b = chat_req(temperature=0.0), chat_req(temperature=0.7)
    assert canonical_hash(a, STRICT_KEYS) != canonical_hash(b, STRICT_KEYS)
    assert canonical_hash(a, LENIENT_KEYS) == canonical_hash(b, LENIENT_KEYS)


def test_store_roundtrip_and_ordering(store):
    t = store.create_trace(Trace(name="t"))
    for i, text in enumerate(["a", "b", "c"]):
        store.add_exchange(
            Exchange.from_request(
                t.id,
                store.next_seq(t.id),
                chat_req(text),
                status_code=200,
                response=chat_resp(text),
            )
        )
    got = store.exchanges(t.id)
    assert [e.seq for e in got] == [0, 1, 2]
    assert got[1].request["messages"][0]["content"] == "b"
    assert store.get_trace(t.id).name == "t"


def test_tool_call_extraction():
    ex = Exchange.from_request(
        "t",
        0,
        chat_req(),
        status_code=200,
        response=chat_resp(tool=("search", '{"q":"x"}')),
    )
    tcs = ex.tool_calls()
    assert len(tcs) == 1 and tcs[0].name == "search" and tcs[0].arguments == '{"q":"x"}'
