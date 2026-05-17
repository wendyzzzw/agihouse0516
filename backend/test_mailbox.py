"""Unit tests for the file-based mailbox. No LLM, no engine — pure transport.

Run:  python3 test_mailbox.py        (exit 0 = pass)
"""
from __future__ import annotations

import os
import sys
import tempfile

from mailbox import Mailbox

_failures = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  OK    {label}")
    else:
        print(f"  FAIL  {label}")
        _failures.append(label)


def _no_tmp_files(root: str) -> bool:
    for dirpath, _, files in os.walk(root):
        if any(f.endswith(".tmp") for f in files):
            return False
    return True


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="mbox_test_")
    # A<->B connected, C isolated from both.
    matrix = {
        "A": {"A": False, "B": True,  "C": False},
        "B": {"A": True,  "B": False, "C": False},
        "C": {"A": False, "B": False, "C": False},
    }
    mb = Mailbox(tmp, matrix, ["A", "B", "C"])

    # 1. send writes exactly one inbox file (recipient) + one outbox file (sender)
    m1 = mb.send("A", "B", "hello B", round_no=1)
    check(m1 is not None, "send across an edge returns the message")
    inbox_b = os.path.join(tmp, "agents", "B", "inbox")
    outbox_a = os.path.join(tmp, "agents", "A", "outbox")
    check(len([f for f in os.listdir(inbox_b) if f.endswith(".json")]) == 1,
          "recipient inbox has exactly 1 unread file")
    check(len(os.listdir(outbox_a)) == 1, "sender outbox has exactly 1 file")

    # 2. matrix enforcement: A->C is not an edge -> nothing written, returns None
    blocked = mb.send("A", "C", "you can't hear this", round_no=1)
    check(blocked is None, "send across a non-edge returns None")
    inbox_c = os.path.join(tmp, "agents", "C", "inbox")
    check(len([f for f in os.listdir(inbox_c) if f.endswith(".json")]) == 0,
          "blocked message wrote nothing to recipient inbox")

    # 3. read_inbox peeks without consuming
    peek = mb.read_inbox("B")
    check(len(peek) == 1 and peek[0]["content"] == "hello B", "read_inbox returns the message")
    check(len(mb.read_inbox("B")) == 1, "read_inbox does NOT consume (still 1 after peek)")

    # 4. ordering: 2nd and 3rd messages drain in send order
    mb.send("A", "B", "second", round_no=2)
    mb.send("A", "B", "third", round_no=3)
    drained = mb.drain_inbox("B")
    check([m["content"] for m in drained] == ["hello B", "second", "third"],
          "drain_inbox returns messages in chronological send order")

    # 5. drain consumes: inbox empty, messages moved to inbox/read/
    check(len(mb.read_inbox("B")) == 0, "inbox empty after drain")
    read_dir = os.path.join(inbox_b, "read")
    check(len([f for f in os.listdir(read_dir) if f.endswith(".json")]) == 3,
          "drained messages preserved in inbox/read/ (Option B history)")

    # 6. thread history is symmetric and complete on both sides
    t_a = mb.thread("A", "B")
    t_b = mb.thread("B", "A")
    check([m["content"] for m in t_a] == ["hello B", "second", "third"],
          "sender thread/<peer> holds full history")
    check([m["content"] for m in t_b] == ["hello B", "second", "third"],
          "recipient thread/<peer> holds full history")

    # 7. atomicity: no .tmp files survive any completed operation
    check(_no_tmp_files(tmp), "no .tmp files left anywhere under the run dir")

    # 8. state snapshot round-trips
    mb.write_state("A", {"id": "A", "goal_status": "verified"})
    state_path = os.path.join(tmp, "agents", "A", "state.json")
    check(os.path.isfile(state_path), "write_state produces agents/A/state.json")

    print()
    if _failures:
        print(f"mailbox: {len(_failures)} FAILURE(S)")
        return 1
    print("mailbox: ALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
