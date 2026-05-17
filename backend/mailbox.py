"""File-based inter-agent communication.

Every message is a file on disk, so a run is fully inspectable (`cat` an inbox)
and the frontend can tail conversation threads. Layout under a run directory:

    runs/live/<run_id>/
      agents/<id>/
        state.json            # agent snapshot (written by the engine)
        inbox/<msgid>.json     # unread messages
        inbox/read/<msgid>.json# messages already drained (Option B: kept as history)
        outbox/<msgid>.json    # every message this agent sent
        thread/<peer>.jsonl    # full append-only conversation with one peer

Design choices:
  - Atomic writes: write `<path>.tmp` then os.replace() — a reader never sees a
    half-written file, and no .tmp turds survive a completed call.
  - Message ids are a zero-padded global sequence, so a lexical sort of inbox
    filenames == chronological order. No clock dependency.
  - send() enforces the comm matrix: a message across a non-edge writes nothing
    and returns None. Topology is enforced at the transport layer.
"""
from __future__ import annotations

import json
import os
import time
from typing import Dict, List, Optional, Any


def _atomic_write_json(path: str, obj: Any) -> None:
    """Write JSON to `path` atomically via a temp file + os.replace()."""
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class Mailbox:
    """File-based message transport for one simulation run."""

    def __init__(self, run_dir: str, matrix: Dict[str, Dict[str, bool]],
                 agent_ids: List[str]):
        self.run_dir = os.path.abspath(run_dir)
        self.matrix = matrix
        self.agent_ids = list(agent_ids)
        self._seq = 0
        self._build_dirs()

    # ---------- paths ----------

    def agent_dir(self, agent_id: str) -> str:
        return os.path.join(self.run_dir, "agents", agent_id)

    def _inbox(self, agent_id: str) -> str:
        return os.path.join(self.agent_dir(agent_id), "inbox")

    def _read_dir(self, agent_id: str) -> str:
        return os.path.join(self._inbox(agent_id), "read")

    def _outbox(self, agent_id: str) -> str:
        return os.path.join(self.agent_dir(agent_id), "outbox")

    def _thread(self, agent_id: str, peer: str) -> str:
        return os.path.join(self.agent_dir(agent_id), "thread", f"{peer}.jsonl")

    def _build_dirs(self) -> None:
        for aid in self.agent_ids:
            for sub in ("inbox/read", "outbox", "thread"):
                os.makedirs(os.path.join(self.agent_dir(aid), sub), exist_ok=True)

    # ---------- send ----------

    def can_send(self, sender: str, recipient: str) -> bool:
        return bool(self.matrix.get(sender, {}).get(recipient, False))

    def send(self, sender: str, recipient: str, content: str,
             round_no: int = 0) -> Optional[dict]:
        """Deliver one message. Returns the message dict, or None if the comm
        matrix forbids this edge (in which case nothing is written to disk)."""
        if not self.can_send(sender, recipient):
            return None
        self._seq += 1
        msg = {
            "id": f"{self._seq:06d}",
            "round": round_no,
            "sender": sender,
            "recipient": recipient,
            "content": content,
            "ts": time.time(),
        }
        fname = f"{msg['id']}.json"
        # recipient's unread inbox + sender's outbox
        _atomic_write_json(os.path.join(self._inbox(recipient), fname), msg)
        _atomic_write_json(os.path.join(self._outbox(sender), fname), msg)
        # append to both sides' conversation thread (history, Option B)
        self._append_thread(sender, recipient, msg)
        self._append_thread(recipient, sender, msg)
        return msg

    def _append_thread(self, owner: str, peer: str, msg: dict) -> None:
        with open(self._thread(owner, peer), "a") as f:
            f.write(json.dumps(msg) + "\n")

    # ---------- receive ----------

    def _inbox_files(self, agent_id: str) -> List[str]:
        """Sorted unread message filenames (the `read/` subdir is skipped)."""
        inbox = self._inbox(agent_id)
        if not os.path.isdir(inbox):
            return []
        return sorted(f for f in os.listdir(inbox) if f.endswith(".json"))

    def read_inbox(self, agent_id: str) -> List[dict]:
        """Return unread messages WITHOUT consuming them (peek)."""
        out = []
        for fn in self._inbox_files(agent_id):
            with open(os.path.join(self._inbox(agent_id), fn)) as f:
                out.append(json.load(f))
        return out

    def drain_inbox(self, agent_id: str) -> List[dict]:
        """Return unread messages and move them to inbox/read/ (Option B:
        consumed but kept as history). Chronological order."""
        msgs = []
        for fn in self._inbox_files(agent_id):
            src = os.path.join(self._inbox(agent_id), fn)
            with open(src) as f:
                msgs.append(json.load(f))
            os.replace(src, os.path.join(self._read_dir(agent_id), fn))
        return msgs

    def thread(self, agent_id: str, peer: str) -> List[dict]:
        """Full conversation history between agent_id and peer."""
        path = self._thread(agent_id, peer)
        if not os.path.isfile(path):
            return []
        with open(path) as f:
            return [json.loads(line) for line in f if line.strip()]

    # ---------- agent state snapshot ----------

    def write_state(self, agent_id: str, state: dict) -> None:
        _atomic_write_json(os.path.join(self.agent_dir(agent_id), "state.json"), state)
