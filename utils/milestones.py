"""§157 run-milestone notifications (phone-friendly progress emails).

The agent already emails once when a block settles (DONE/FAILED). A PD run is
hours long, so this adds a few SHORT status emails at the checkpoints that
decide whether the run is worth letting continue:

  start      first ORFS stage actually executed (resume point)
  io_shadow  §156 IO macro-shadow hook verdict (only for blocks whose config
             uses the hook)
  hold       hold buffers inserted at CTS (+ RSZ-0064 if hold not repaired)
  grt        global-route overflow per layer, or GRT-0116
  drt        detailed-route: violations after the 1st iteration, final count
  error      first real [ERROR] line (known-harmless hook noise excluded)

Pure line parser: MilestoneTracker.feed(line, stage) returns a list of
(key, subject, body) tuples, each key emitted at most once per block run. The
agent does the sending (best-effort, background thread).

Env only (no CLI flags): JANUS_NOTIFY_MILESTONES = 1 (default, all blocks) |
0 (off) | comma list of block names (only those).
"""

from __future__ import annotations

import os
import re
from typing import List, Optional, Tuple

Event = Tuple[str, str, str]

# Errors seen in every run from report hooks; never a real failure.
_BENIGN_ERRORS = ("GUI-0056", "STA-2204")

_RE_SHADOW = re.compile(r"JANUS_IO_SHADOW:\s*(.*)")
_RE_HOLD = re.compile(r"RSZ-0032\] Inserted (\d+) hold buffers")
_RE_LAYER = re.compile(r"^(M\d+|Total)\s+\d+\s+\d+\s+[\d.]+%\s+(\d+)\s*/\s*(\d+)\s*/\s*(\d+)")
_RE_DRT_VIOL = re.compile(r"DRT-0199\]\s+Number of violations = (\d+)")
_RE_ERROR = re.compile(r"^\[ERROR [A-Z]+-\d+\]|^Error: ")


def milestones_enabled(block: str, env: Optional[dict] = None) -> bool:
    v = (env if env is not None else os.environ).get("JANUS_NOTIFY_MILESTONES", "1").strip()
    if v in ("", "1", "all", "on", "yes"):
        return True
    if v in ("0", "off", "no", "none"):
        return False
    return block in {b.strip() for b in v.split(",") if b.strip()}


def config_uses_io_shadow(config_path: str) -> bool:
    try:
        with open(config_path, encoding="utf-8", errors="replace") as fh:
            return "janus_io_macro_shadow" in fh.read()
    except OSError:
        return False


class MilestoneTracker:
    def __init__(self, block: str, expect_io_shadow: bool = False) -> None:
        self.block = block
        self.expect_io_shadow = expect_io_shadow
        self.sent: set = set()
        self.first_stage: Optional[str] = None
        self.in_io = False
        self.io_done = False
        self.shadow: List[str] = []
        self.hold_unrepaired = False
        self.in_cong = False
        self.cong: List[str] = []
        self.drt_viol: Optional[int] = None
        self.drt_iters = 0

    def _ev(self, out: List[Event], key: str, tag: str, msg: str, body: str = "") -> None:
        if key in self.sent:
            return
        self.sent.add(key)
        out.append((key, f"[JANUS] {self.block}: {tag} {msg}", body or msg))

    def _io_verdict(self, out: List[Event]) -> None:
        if self.io_done:
            return
        self.io_done = True
        bad = [s for s in self.shadow if re.search(r"WARN|ERROR|disabled|nothing excluded", s)]
        excl = [s for s in self.shadow if "exclude" in s and s not in bad]
        body = "\n".join(self.shadow) or "(no JANUS_IO_SHADOW lines in the log)"
        if bad:
            self._ev(out, "io_shadow", "FAIL", "IO shadow hook did not apply excludes", body)
        elif excl:
            self._ev(out, "io_shadow", "OK", f"IO shadow hook applied ({len(excl)} edge excludes)", body)
        elif self.expect_io_shadow:
            self._ev(out, "io_shadow", "FAIL",
                     "io_placement ran with NO JANUS_IO_SHADOW output (hook not loaded? "
                     "re-run 03_setup_janus_configs.sh)", body)

    def feed(self, line: str, stage: Optional[str]) -> List[Event]:
        out: List[Event] = []
        st = stage or ""
        if st and self.first_stage is None:
            self.first_stage = st
            rebuild = bool(re.match(r"1_synth|synth|yosys", st))
            self._ev(out, "start", "WARN" if rebuild else "OK",
                     f"started at '{st}'" + (" (full rebuild from synthesis)" if rebuild
                                             else " (earlier stages reused)"))
        if "io_placement" in st:
            self.in_io = True
        elif self.in_io and st:
            self.in_io = False
            self._io_verdict(out)

        m = _RE_SHADOW.search(line)
        if m:
            self.shadow.append(m.group(1).strip())
            return out
        if "RSZ-0064]" in line:
            self.hold_unrepaired = True
            return out
        m = _RE_HOLD.search(line)
        if m:
            n = int(m.group(1))
            tag = "OK" if n < 1000 and not self.hold_unrepaired else "WARN"
            self._ev(out, "hold", tag, f"CTS inserted {n} hold buffers"
                     + (" (RSZ-0064: hold NOT fully repaired)" if self.hold_unrepaired else ""))
            return out
        if "GRT-0096] Final congestion report" in line:
            self.in_cong, self.cong = True, []
            return out
        if self.in_cong:
            m = _RE_LAYER.match(line.strip())
            if m:
                self.cong.append(f"{m.group(1)}: overflow {m.group(4)}")
                if m.group(1) == "Total":
                    self.in_cong = False
                    tot = int(m.group(4))
                    body = "\n".join(self.cong)
                    if tot == 0:
                        self._ev(out, "grt", "OK", "global route clean (overflow 0)", body)
                    else:
                        hot = ", ".join(c for c in self.cong[:-1] if not c.endswith(" 0"))
                        self._ev(out, "grt", "FAIL", f"global route overflow {tot} ({hot})", body)
                        self.sent.add("error")
            return out
        if "GRT-0116]" in line:
            self._ev(out, "grt", "FAIL", "GRT-0116 global route congestion", "\n".join(self.cong) or line)
            self.sent.add("error")  # same failure; no duplicate error mail
            return out
        m = _RE_DRT_VIOL.search(line)
        if m:
            self.drt_viol = int(m.group(1))
            self.drt_iters += 1
            if self.drt_iters == 1:
                self._ev(out, "drt_start", "INFO",
                         f"detailed route running: {self.drt_viol} violations after iteration 1")
            return out
        if "DRT-0198] Complete detail routing" in line:
            v = self.drt_viol
            self._ev(out, "drt_done", "OK" if v == 0 else "WARN",
                     f"detailed route complete: {v if v is not None else '?'} violations "
                     f"after {self.drt_iters} iterations")
            return out
        if _RE_ERROR.search(line) and not any(b in line for b in _BENIGN_ERRORS):
            self._ev(out, "error", "FAIL", f"error at '{st or '?'}': {line.strip()[:160]}")
        return out

    def finish(self) -> List[Event]:
        """Flush verdicts that depend on a stage boundary never seen (e.g. run
        died inside io_placement)."""
        out: List[Event] = []
        if self.in_io:
            self._io_verdict(out)
        return out
