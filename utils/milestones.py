"""§157 run-milestone notifications (phone-friendly progress emails).

The agent already emails once when a block settles (DONE/FAILED). A PD run is
hours long, so this adds a few SHORT status emails at the checkpoints that
decide whether the run is worth letting continue:

  start      first ORFS stage actually executed (resume point)
  io_shadow  §156 IO macro-shadow hook verdict (only for blocks whose config
             uses the hook)
  hold       hold buffers inserted at CTS (+ RSZ-0064 if hold not repaired)
  detour     §158 macro-detour hook: nets pre-buffered around the macro before
             GRT (WARN if it skipped a straddling net)
  grt        global-route overflow per layer, or GRT-0116
  drt        detailed-route: violations after the 1st iteration, final count
  error      first real [ERROR] line (known-harmless hook noise excluded)

Pure line parser: MilestoneTracker.feed(line, stage) returns a list of
(key, subject, body) tuples, each key emitted at most once per block run. The
agent does the sending (best-effort, background thread).

Env only (no CLI flags): JANUS_NOTIFY_MILESTONES = 1 (default, all blocks) |
0 (off) | comma list of block names (only those).

§175: the per-event emails above were user_core bring-up debug aids and are
now OFF by default -- they are still parsed and logged to notify.log, but only
emailed with JANUS_NOTIFY_DEBUG=1 (see is_debug_event). What IS emailed:
  * one stage-table email per completed ORFS stage (track_stage() reports the
    stage that just finished; the agent renders it with utils/stage_report.py)
  * KILL red flags, immediately, while repair_timing is still running:
    pre-repair hold WNS < -300 ps, or > 5000 hold buffers.
"""

from __future__ import annotations

import os
import re
from typing import List, Optional, Tuple

Event = Tuple[str, str, str]

# Errors seen in every run from report hooks; never a real failure.
_BENIGN_ERRORS = ("GUI-0056", "STA-2204")

_RE_SHADOW = re.compile(r"JANUS_IO_SHADOW:\s*(.*)")
_RE_DETOUR_N = re.compile(r"JANUS_DETOUR: (\d+) wrap-around net")
_RE_HOLD = re.compile(r"RSZ-0032\] Inserted (\d+) hold buffers")
_RE_LAYER = re.compile(r"^(M\d+|Total)\s+\d+\s+\d+\s+[\d.]+%\s+(\d+)\s*/\s*(\d+)\s*/\s*(\d+)")
_RE_DRT_VIOL = re.compile(r"DRT-0199\]\s+Number of violations = (\d+)")
_RE_ERROR = re.compile(r"^\[ERROR [A-Z]+-\d+\]|^Error: ")

# §175 user kill rule (kept in sync with stage_report.HOLD_*_KILL).
HOLD_BUF_KILL = 5000
HOLD_PRE_KILL_PS = -300.0
# Events emailed regardless of JANUS_NOTIFY_DEBUG; everything else is debug.
ALWAYS_KEYS = frozenset({"kill_hold_pre", "kill_hold_bufs"})


def is_debug_event(key: str) -> bool:
    """§175: True for the legacy per-event (user_core debug) milestones."""
    return key not in ALWAYS_KEYS


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
        self.detour: List[str] = []
        # §175 live hold-repair watch + stage-completion tracking
        self.hold_mode = False
        self.hold_cols: List[str] = []
        self.stage_key: Optional[str] = None
        self.stages_run: List[str] = []

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
        excl = [s for s in self.shadow if "-> exclude" in s and s not in bad]
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
        if "JANUS_DETOUR:" in line:
            msg = line.split("JANUS_DETOUR:", 1)[1].strip()
            if msg.startswith(("net ", "WARN")):
                self.detour.append(msg)
            m = _RE_DETOUR_N.search(line)
            if m:
                n = int(m.group(1))
                warn = any(d.startswith("WARN") for d in self.detour)
                self._ev(out, "detour", "WARN" if warn else "OK",
                         f"macro detour pre-buffered {n} net(s) before GRT"
                         + (" (some straddling nets skipped)" if warn else ""),
                         "\n".join(self.detour[:40]) or msg)
            return out
        if "RSZ-0064]" in line:
            self.hold_unrepaired = True
            return out
        self._hold_watch(line, st, out)
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

    def _hold_watch(self, line: str, st: str, out: List[Event]) -> None:
        """§175: parse repair_timing's hold table live; fire KILL events."""
        if "RSZ-0046]" in line:
            self.hold_mode, self.hold_cols = True, []
            return
        if "RSZ-0094]" in line or "RSZ-0032]" in line:
            m = _RE_HOLD.search(line)
            if m and int(m.group(1)) > HOLD_BUF_KILL:
                self._ev(out, "kill_hold_bufs", "KILL RUN?",
                         f"{m.group(1)} hold buffers inserted at '{st or '?'}' "
                         f"(> {HOLD_BUF_KILL})")
            self.hold_mode = False
            return
        if not self.hold_mode or "|" not in line:
            return
        cells = [c.strip() for c in line.split("|")]
        if "WNS" in cells and cells[0].startswith("Iter"):
            self.hold_cols = cells
            return
        if not self.hold_cols or not (cells[0].isdigit() or cells[0] == "final"):
            return
        try:
            wns = float(cells[self.hold_cols.index("WNS")])
        except (ValueError, IndexError):
            wns = None
        if cells[0] == "0" and wns is not None and wns < HOLD_PRE_KILL_PS:
            self._ev(out, "kill_hold_pre", "KILL RUN?",
                     f"pre-repair hold WNS {wns:.1f} ps at '{st or '?'}' "
                     f"(< {HOLD_PRE_KILL_PS:.0f} ps)")
        for name in ("Buffers", "Inserted"):
            if name in self.hold_cols:
                try:
                    b = int(cells[self.hold_cols.index(name)])
                except (ValueError, IndexError):
                    break
                if b > HOLD_BUF_KILL:
                    self._ev(out, "kill_hold_bufs", "KILL RUN?",
                             f"hold repair already at {b} buffers at '{st or '?'}' "
                             f"(> {HOLD_BUF_KILL}) and still going")
                break

    def track_stage(self, key: Optional[str]) -> Optional[str]:
        """§175: feed the current stage key (stage_report.stage_key); returns
        the key of the stage that just COMPLETED (moved forward past it),
        else None. Each stage is reported at most once per make run."""
        if not key or key == self.stage_key:
            return None
        order = ["1_synth", "2_floorplan", "3_place", "4_cts", "5_grt", "5_drt", "6_finish"]
        prev = self.stage_key
        if prev in order and key in order and order.index(key) < order.index(prev):
            return None   # sub-step label regressed; ignore
        self.stage_key = key
        self.stages_run.append(key)
        if prev and ("stage_done:" + prev) not in self.sent:
            self.sent.add("stage_done:" + prev)
            return prev
        return None

    def finish(self) -> List[Event]:
        """Flush verdicts that depend on a stage boundary never seen (e.g. run
        died inside io_placement)."""
        out: List[Event] = []
        if self.in_io:
            self._io_verdict(out)
        return out
