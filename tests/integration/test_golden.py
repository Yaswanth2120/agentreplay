import asyncio
from pathlib import Path

import httpx
import pytest

from agentreplay.exportio import import_trace, load_trace
from agentreplay.proxy import create_app
from agentreplay.store import TraceStore
from agentreplay.tools import ToolRecorder, ToolReplayMiss
from examples.minimal_agent import run_agent
from scripts.make_golden import generate_golden, main

GOLDEN = Path(__file__).parents[1] / "golden"


def test_golden_generation_is_reproducible_and_matches_checked_files(
    tmp_path: Path,
) -> None:
    first = generate_golden(tmp_path / "first")
    second = generate_golden(tmp_path / "second")
    assert len(first) == len(second) == 2
    assert sorted(path.name for path in GOLDEN.glob("*.jsonl")) == sorted(
        path.name for path in first
    )
    for left, right in zip(first, second, strict=True):
        assert (
            left.read_bytes() == right.read_bytes() == (GOLDEN / left.name).read_bytes()
        )
        trace, exchanges, events = load_trace(left)
        assert trace.agent_name == "minimal_agent"
        assert len(exchanges) == len(events) + 1


@pytest.mark.parametrize(
    "filename", ["minimal-add.jsonl", "minimal-add-repeated.jsonl"]
)
def test_golden_runs_the_unmodified_agent_offline(
    filename: str, tmp_path: Path
) -> None:
    async def exercise() -> None:
        store = TraceStore(tmp_path / "golden.db")
        try:
            trace = import_trace(store, GOLDEN / filename)
            app = create_app(store, "replay", trace_id=trace.id)
            recorder = ToolRecorder(store, trace.id, "replay")
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://offline",
                ) as client,
            ):
                result = await run_agent(
                    client, recorder, lambda **kwargs: pytest.fail("executed tool")
                )
            assert result == "2 + 3 = 5" and app.state.replayer.remaining == 0
            assert store.findings(app.state.run_id) == []
            with pytest.raises(ToolReplayMiss):
                recorder.call(
                    "add",
                    {"a": 2, "b": 3},
                    lambda **kwargs: pytest.fail("executed tool"),
                )
        finally:
            store.close()

    asyncio.run(exercise())


def test_generation_cli_uses_requested_directory(tmp_path: Path) -> None:
    destination = tmp_path / "cli-golden"
    main(["--out", str(destination)])
    for checked in GOLDEN.glob("*.jsonl"):
        assert (destination / checked.name).read_bytes() == checked.read_bytes()
