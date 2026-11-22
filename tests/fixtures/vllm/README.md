# Actual vLLM streaming fixture

`qwen3-8b-stream.jsonl` was recorded on the owner-provided A100-SXM4 80 GB host
with Qwen/Qwen3-8B revision b968826d9c46dd6066d109eabc6255188de91218, vLLM 0.32.0,
and the settings in examples/eval-vast-a100.yaml. Recording code commit:
92874a4. It contains 20 original SSE data lines, the reassembled completion, and
the executed calculate(add, 2, 3) event with result 5. No request headers are stored.

The integration test imports and replays these actual bytes offline with the
recorded tool implementation forbidden from executing. This fixture is separate
from tests/golden because that directory's check command uses the different
minimal-agent/add protocol. This is a compatibility fixture, not a live success
rate or a substitute for the owner's task/label review.
