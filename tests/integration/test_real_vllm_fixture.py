"""Offline replay of an actual Qwen3/vLLM streaming capture from the M9 host."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentreplay.exportio import import_trace
from agentreplay.proxy import create_app
from agentreplay.store import TraceStore
from agentreplay.streaming import reassemble
from agentreplay.tools import ToolRecorder


def test_actual_vllm_stream_and_tool_result_replay_without_execution(
    store: TraceStore,
) -> None:
    fixture = Path(__file__).parents[1] / "fixtures" / "vllm" / "qwen3-8b-stream.jsonl"
    trace = import_trace(store, fixture)
    exchange = store.exchanges(trace.id)[0]
    event = store.tool_events(trace.id)[0]
    assert exchange.stream and len(exchange.chunks) > 2
    assert reassemble(exchange.chunks) == exchange.response
    assert exchange.tool_calls()[0].name == "calculate"
    assert event.args == {"op": "add", "a": 2, "b": 3} and event.result == 5
    app = create_app(store, "replay", trace_id=trace.id)
    with TestClient(app) as client:
        result = client.post("/v1/chat/completions", json=exchange.request)
        assert result.status_code == 200
        assert result.text == "\n\n".join(exchange.chunks) + "\n\n"
        recorder = ToolRecorder(store, trace.id, "replay", session=app.state.session)
        assert (
            recorder.call(
                "calculate",
                event.args,
                lambda **kwargs: pytest.fail("tool executed during replay"),
            )
            == 5
        )
        finished = client.post(f"/_agentreplay/runs/{app.state.run_id}/finish", json={})
        assert finished.status_code == 200 and finished.json()["findings"] == []
