"""Reads an SSE stream on stdin, timestamps each frame, asserts it is LIVE.

Used by check_stream.sh:  curl -sN <sse-url> | python3 stream_analyze.py <delay>
A burst (fake-live) stream fails the timing checks; a genuinely paced one passes.
"""
import sys
import time
import json

t0 = time.time()
frames = []                       # (arrival_offset_seconds, payload_str)
for line in sys.stdin:
    line = line.strip()
    if line.startswith("data:"):
        frames.append((time.time() - t0, line[5:].strip()))

ok = True


def check(cond, label):
    global ok
    print(f"  {'OK  ' if cond else 'FAIL'}  {label}")
    if not cond:
        ok = False


n = len(frames)
check(n > 100, f"received many SSE frames (n={n})")
if n == 0:
    print("\nstream: FAILURES ABOVE")
    sys.exit(1)

kinds = []
for _, payload in frames:
    try:
        kinds.append(json.loads(payload).get("kind"))
    except Exception:
        kinds.append(None)

check(kinds[0] == "replay_start", "first frame is replay_start")
check(kinds[-1] == "replay_end", "last frame is replay_end")

span = frames[-1][0] - frames[0][0]
check(span > 3.0, f"frames span > 3s of wall-clock (span={span:.1f}s) — stream is paced")

# Burst test: a fake 'live' stream dumps everything at once. The midpoint frame
# must NOT have arrived in the first second.
mid_offset = frames[n // 2][0]
check(mid_offset > 1.0, f"midpoint frame arrived late (t={mid_offset:.1f}s) — not a burst")

print()
print("stream: ALL OK" if ok else "stream: FAILURES ABOVE")
sys.exit(0 if ok else 1)
