import socket

import pytest

from agentreplay.store import TraceStore

# Pre-existing untracked copies are preserved locally, not part of the test suite.
collect_ignore = [
    "test_checks 2.py",
    "test_replay_m4 2.py",
    "integration/test_run_routing 2.py",
]


@pytest.fixture
def store():
    s = TraceStore(":memory:")
    yield s
    s.close()


def chat_req(content="hi", **kw):
    return {
        "model": "qwen3-8b",
        "messages": [{"role": "user", "content": content}],
        **kw,
    }


def chat_resp(text="hello", tool=None):
    msg = {"role": "assistant", "content": text}
    if tool:
        msg["tool_calls"] = [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": tool[0], "arguments": tool[1]},
            }
        ]
    return {
        "id": "x",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
    }


@pytest.fixture(autouse=True)
def disable_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_connect(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "Tests must use mock transports; network access is disabled"
        )

    monkeypatch.setattr(socket.socket, "connect", fail_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", fail_connect)
    monkeypatch.setattr(socket, "create_connection", fail_connect)
