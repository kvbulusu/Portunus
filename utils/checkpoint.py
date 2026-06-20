"""
checkpoint.py — Persist / resume the autonomous agent's state.

The agent can run for hours (overnight). If it is interrupted (Ctrl-C, WSL2
sleep, power loss) we want to resume without rebuilding finished blocks. State is
a small JSON file written atomically after every meaningful transition.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from typing import Dict, Any


@dataclass
class Checkpoint:
    path: str
    state: Dict[str, Any] = field(default_factory=dict)

    # ---- load / save --------------------------------------------------------
    def load(self) -> Dict[str, Any]:
        if os.path.isfile(self.path):
            try:
                with open(self.path) as fh:
                    self.state = json.load(fh)
            except (json.JSONDecodeError, OSError):
                self.state = {}
        return self.state

    def save(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self.state["_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        # Atomic write: temp file + rename so an interrupted write never corrupts.
        d = os.path.dirname(os.path.abspath(self.path))
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(self.state, fh, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    # ---- convenience accessors ---------------------------------------------
    def set_block(self, block: str, **kwargs) -> None:
        blocks = self.state.setdefault("blocks", {})
        rec = blocks.setdefault(block, {})
        rec.update(kwargs)
        self.save()

    def get_block(self, block: str) -> Dict[str, Any]:
        return self.state.get("blocks", {}).get(block, {})

    def block_status(self, block: str) -> str:
        return self.get_block(block).get("status", "PENDING")

    def is_done(self, block: str) -> bool:
        return self.block_status(block) == "DONE"

    def record_usage(self, **kwargs) -> None:
        self.state.setdefault("usage", {}).update(kwargs)
        self.save()

    def reset(self) -> None:
        self.state = {}
        if os.path.exists(self.path):
            os.remove(self.path)


# ---- self-test --------------------------------------------------------------
if __name__ == "__main__":
    import tempfile as _tf
    p = os.path.join(_tf.mkdtemp(), "state.json")
    cp = Checkpoint(p)
    cp.load()
    cp.set_block("security", status="DONE", attempts=1)
    cp.set_block("noc", status="RUNNING")
    cp2 = Checkpoint(p)
    cp2.load()
    assert cp2.is_done("security")
    assert cp2.block_status("noc") == "RUNNING"
    print("checkpoint self-test OK:", cp2.state["blocks"])
