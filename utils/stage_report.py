"""§175 stage-table notification emails (what a PD lead wants on a phone).

Every JANUS email (per-stage milestone, final DONE, FAILED) carries ONE
cumulative table of all ORFS stages:

    stage | status | wall | CPU | peak mem | key QoR

plus a narrow QoR-detail table, red flags, run provenance and (on failure) the
first ERROR lines + the last N lines of the failing stage log.

Data sources (all best-effort; a missing value renders as '—'):
  * ORFS logs  logs/<plat>/<design>/<variant>/<N>_*.log
      'Elapsed time: [h:]m:s[h:]min:sec. CPU time: user U sys S (P%). Peak memory: K KB.'
      repair_timing tables (pre/post setup+hold WNS, hold buffers), RSZ-0032,
      GRT-0096 overflow, DRT-0199 violations, ANT-000x, [WARNING/ERROR XXX-NNNN]
  * ORFS metrics json  logs/.../<N>_*.json  (<stage>__timing__setup__ws, ...)
  * stage reports  reports/.../4_cts_final.rpt etc. (tns/wns, viol counts,
      skew, worst paths)

Pure functions; nothing here sends mail. build_report() never raises.

Env knobs:
  JANUS_NOTIFY_TAIL_LINES  failing-log tail length (default 50)
  JANUS_NOTIFY_DEBUG=1     re-enable the old per-event debug milestone emails
                           (io_shadow / detour / hold / grt / drt / error)
"""

from __future__ import annotations

import glob
import html as _h
import json
import os
import re
from collections import Counter
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

DASH = "—"
DEFAULT_TAIL_LINES = 50

# Red-flag thresholds. §177 (user rule revised 2026-10-30: "fewer buffers is
# better, but hold must close"): KILL only if hold buffers > 10000, or hold is
# still negative after repair. Pre-repair hold < -300 ps / > 5000 buffers is
# now a WARN (RED), not a kill signal.
HOLD_BUF_KILL = 10000
HOLD_BUF_WARN = 5000
HOLD_PRE_KILL_PS = -300.0  # kept for import compat; now a WARN threshold

# key, label, log prefixes, metric prefixes, reports, result artifacts
STAGES: List[Tuple[str, str, Tuple[str, ...], Tuple[str, ...], Tuple[str, ...], Tuple[str, ...]]] = [
    ("1_synth", "synth", ("1_",), ("synth",), ("synth_stat.txt",),
     ("1_synth.v", "1_2_yosys.v", "1_synth.odb")),
    ("2_floorplan", "floorplan", ("2_",), ("floorplan",), ("2_floorplan_final.rpt",),
     ("2_floorplan.odb",)),
    ("3_place", "place", ("3_",), ("globalplace", "placeopt", "detailedplace"),
     ("3_detailed_place.rpt", "3_resizer.rpt"), ("3_place.odb",)),
    ("4_cts", "cts", ("4_",), ("cts",), ("4_cts_final.rpt",), ("4_cts.odb",)),
    ("5_grt", "grt", ("5_1_",), ("globalroute",), ("5_global_route.rpt",),
     ("5_1_grt.odb",)),
    ("5_drt", "drt", ("5_2_", "5_3_"), ("detailedroute",), ("5_route_drc.rpt",),
     ("5_route.odb", "5_2_route.odb")),
    ("6_finish", "finish", ("6_",), ("finish",), ("6_finish.rpt",),
     ("6_final.odb",)),
]
STAGE_KEYS = [s[0] for s in STAGES]
_SPEC = {s[0]: s for s in STAGES}


# ---------------------------------------------------------------------------
# env knobs
# ---------------------------------------------------------------------------
def notify_debug_enabled(env: Optional[dict] = None) -> bool:
    v = (env if env is not None else os.environ).get("JANUS_NOTIFY_DEBUG", "0")
    return v.strip().lower() in ("1", "on", "yes", "true", "all")


def tail_lines_setting(env: Optional[dict] = None) -> int:
    try:
        n = int((env if env is not None else os.environ).get(
            "JANUS_NOTIFY_TAIL_LINES", DEFAULT_TAIL_LINES))
        return n if n > 0 else DEFAULT_TAIL_LINES
    except (TypeError, ValueError):
        return DEFAULT_TAIL_LINES


# ---------------------------------------------------------------------------
# stage mapping
# ---------------------------------------------------------------------------
def stage_key(label: Optional[str], line: str = "", major: int = 0) -> Optional[str]:
    """Map an agent stage label (+ raw line / major index) to a STAGES key."""
    s = (label or "").lower()
    n = major
    if not n and s[:1].isdigit():
        try:
            n = int(s.split("_", 1)[0])
        except ValueError:
            n = 0
    if not (1 <= n <= 6):
        return None
    if n == 5:
        low = (line or "").lower()
        if ("detail" in s or "5_2" in s or "5_3" in s or "drt-" in low
                or "detail_route" in low or "detailed_route" in low or "5_2_route" in low):
            return "5_drt"
        return "5_grt"
    return {1: "1_synth", 2: "2_floorplan", 3: "3_place", 4: "4_cts",
            6: "6_finish"}[n]


# ---------------------------------------------------------------------------
# primitive parsers
# ---------------------------------------------------------------------------
_RE_ELAPSED = re.compile(
    r"Elapsed time:\s*([\d:.]+)\s*\[h:\]min:sec\.\s*CPU time:\s*user\s*([\d.]+)\s*"
    r"sys\s*([\d.]+)\s*\(\s*(\d+)%\)\.\s*Peak memory:\s*(\d+)\s*KB")
_RE_MSG = re.compile(r"\[(WARNING|ERROR|FATAL)\s+([A-Z]{2,6}-\d{3,4})\]")
_RE_ERRLINE = re.compile(r"\[(ERROR|FATAL)\s+[A-Z]{2,6}-\d+\]|^\s*Error:|^ERROR:|"
                         r"make.*\*\*\*.*Error\s+\d+|Command terminated by signal|\bKilled\b")
_BENIGN = ("GUI-0056", "STA-2204")
_RE_HOLD_INS = re.compile(r"RSZ-0032\]\s+Inserted\s+(\d+)\s+hold buffers")
_RE_BUF_INS = re.compile(r"RSZ-00(?:45|48)\]\s+Inserted\s+(\d+)\s+buffers")
_RE_DRT = re.compile(r"DRT-0199\]\s+Number of violations\s*=\s*(\d+)")
_RE_GRT_TOT = re.compile(r"^Total\s+\d+\s+\d+\s+[\d.]+%\s+(\d+)\s*/\s*(\d+)\s*/\s*(\d+)")
_RE_ANT = re.compile(r"ANT-000[12]\]\s+Found\s+(\d+)\s+net violations")
_RE_MOFS_D = re.compile(r"JANUS_CTS_MACRO_OFS:.*?\bD\s+(-?[\d.]+)")


def _wall_s(s: str) -> Optional[float]:
    try:
        parts = [float(p) for p in s.strip().split(":")]
    except ValueError:
        return None
    sec = 0.0
    for p in parts:
        sec = sec * 60 + p
    return sec


def parse_elapsed(text: str) -> List[Dict[str, float]]:
    """Every ORFS 'Elapsed time ... Peak memory' banner in *text*."""
    out = []
    for m in _RE_ELAPSED.finditer(text or ""):
        w = _wall_s(m.group(1))
        if w is None:
            continue
        out.append({"wall": w, "cpu": float(m.group(2)) + float(m.group(3)),
                    "cpu_pct": int(m.group(4)), "peak_kb": int(m.group(5))})
    return out


def fmt_dur(sec: Optional[float]) -> str:
    if sec is None:
        return DASH
    sec = int(round(sec))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def fmt_mem(kb: Optional[float]) -> str:
    if not kb:
        return DASH
    g = kb / 1048576.0
    return f"{g:.1f}G" if g >= 1 else f"{kb / 1024.0:.0f}M"


def fmt_ps(v, signed: bool = True) -> str:
    if v is None:
        return DASH
    try:
        f = float(v)
    except (TypeError, ValueError):
        return DASH
    a = abs(f)
    if a >= 1e6:
        s = f"{f / 1e6:.1f}M"
    elif a >= 1e4:
        s = f"{f / 1e3:.0f}k"
    elif a >= 100:
        s = f"{f:.0f}"
    else:
        s = f"{f:.1f}"
    return ("+" + s) if (signed and f > 0) else s


def fmt_int(v) -> str:
    if v is None:
        return DASH
    try:
        n = int(v)
    except (TypeError, ValueError):
        return DASH
    if abs(n) >= 1_000_000:
        return f"{n / 1e6:.1f}M"
    if abs(n) >= 10_000:
        return f"{n / 1e3:.0f}k"
    return str(n)


def _read(path: Optional[str], limit: int = 16 << 20) -> str:
    if not path:
        return ""
    try:
        with open(path, "rb") as fh:
            return fh.read(limit).decode("utf-8", "replace")
    except OSError:
        return ""


def read_tail(path: Optional[str], n: int, max_bytes: int = 1 << 20) -> List[str]:
    if not path or n <= 0:
        return []
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    return data.splitlines()[-n:]


def _fresh(path: str, since: Optional[float]) -> bool:
    """§195: file written by THIS run (mtime >= since). since=None -> always."""
    if since is None:
        return True
    try:
        return os.path.getmtime(path) >= since
    except OSError:
        return False


def stage_logs(logs_dir: Optional[str], key: str, since: Optional[float] = None) -> List[str]:
    if not logs_dir or not os.path.isdir(logs_dir):
        return []
    out = []
    for pre in _SPEC[key][2]:
        out += glob.glob(os.path.join(logs_dir, pre + "*.log"))
    return sorted(p for p in set(out) if _fresh(p, since))


def stage_runtime(logs_dir: Optional[str], key: str,
                  since: Optional[float] = None) -> Dict[str, Optional[float]]:
    """Sum wall/CPU and max peak memory over the stage's sub-step logs."""
    wall = cpu = 0.0
    peak = 0
    n = 0
    for lp in stage_logs(logs_dir, key, since):
        for e in parse_elapsed(_read_tail_text(lp)):
            wall += e["wall"]
            cpu += e["cpu"]
            peak = max(peak, e["peak_kb"])
            n += 1
    if not n:
        return {"wall": None, "cpu": None, "peak_kb": None, "n": 0}
    return {"wall": wall, "cpu": cpu, "peak_kb": peak, "n": n}


def _read_tail_text(path: str) -> str:
    # The banner is the last line of each ORFS log; 64 KB tail is plenty.
    return "\n".join(read_tail(path, 400, 64 << 10))


def load_metrics(logs_dir: Optional[str], since: Optional[float] = None) -> Dict[str, object]:
    m: Dict[str, object] = {}
    if not logs_dir or not os.path.isdir(logs_dir):
        return m
    for jp in sorted(glob.glob(os.path.join(logs_dir, "[0-9]_*.json"))):
        if not _fresh(jp, since):
            continue
        try:
            with open(jp) as fh:
                d = json.load(fh)
            if isinstance(d, dict):
                m.update(d)
        except (OSError, ValueError):
            continue
    return m


def metric(m: Dict[str, object], prefixes: Sequence[str], suffix: str):
    """Last prefix (latest sub-step) that has <prefix>__<suffix>."""
    for p in reversed(list(prefixes)):
        v = m.get(f"{p}__{suffix}")
        if v is not None:
            return v
    return None


def parse_stage_rpt(text: str) -> Dict[str, object]:
    """ORFS '<stage> final' report: tns/wns, viol counts, skew."""
    r: Dict[str, object] = {}
    pats = {
        "setup_tns": r"^tns max\s+(-?[\d.]+)", "setup_ws": r"^wns max\s+(-?[\d.]+)",
        "hold_tns": r"^tns min\s+(-?[\d.]+)", "hold_ws": r"^wns min\s+(-?[\d.]+)",
        "setup_viol": r"^setup violation count\s+(\d+)",
        "hold_viol": r"^hold violation count\s+(\d+)",
        "slew_viol": r"^max slew violation count\s+(\d+)",
        "cap_viol": r"^max cap violation count\s+(\d+)",
        "fanout_viol": r"^max fanout violation count\s+(\d+)",
        "skew": r"^\s*(-?[\d.]+)\s+setup skew",
    }
    for k, p in pats.items():
        mm = re.search(p, text or "", re.M)
        if mm:
            r[k] = float(mm.group(1))
    return r


def parse_paths(text: str, n: int = 5) -> Dict[str, List[Tuple[str, str, float]]]:
    """Top-n worst (start, end, slack) per path type from report_checks text.
    Holds only strings/floats (never tool objects)."""
    out: Dict[str, List[Tuple[str, str, float]]] = {"max": [], "min": []}
    sp = ep = None
    pt = "max"
    for line in (text or "").splitlines():
        s = line.strip()
        if s.startswith("Startpoint:"):
            sp, ep = s.split(":", 1)[1].split(" (")[0].strip(), None
        elif s.startswith("Endpoint:"):
            ep = s.split(":", 1)[1].split(" (")[0].strip()
        elif s.startswith("Path Type:"):
            pt = "min" if "min" in s else "max"
        elif s.endswith(("slack (VIOLATED)", "slack (MET)")) and sp and ep:
            try:
                out[pt].append((sp, ep, float(s.split()[0])))
            except (ValueError, IndexError):
                pass
            sp = ep = None
    for k in out:
        seen, uniq = set(), []
        for p in sorted(out[k], key=lambda x: x[2]):
            if (p[0], p[1]) not in seen:
                seen.add((p[0], p[1]))
                uniq.append(p)
        out[k] = uniq[:n]
    return out


def parse_repair(text: str) -> Dict[str, object]:
    """repair_timing progress tables: pre/post WNS for setup and hold, hold
    buffers (RSZ-0032 or the running 'Buffers' column), RSZ-0060 cap hit."""
    r: Dict[str, object] = {}
    mode = None
    cols: List[str] = []
    for line in (text or "").splitlines():
        if "RSZ-0094]" in line or "RSZ-0099]" in line:
            mode = "setup"
            continue
        if "RSZ-0046]" in line:
            mode = "hold"
            continue
        if "|" in line and ("WNS" in line) and ("Iter" in line):
            cols = [c.strip() for c in line.split("|")]
            continue
        if mode and cols and "|" in line:
            cells = [c.strip() for c in line.split("|")]
            if not cells or cells[0] not in ("0", "final") and not cells[0].isdigit():
                continue
            try:
                wns = float(cells[cols.index("WNS")])
            except (ValueError, IndexError):
                wns = None
            if cells[0] == "0" and wns is not None:
                r.setdefault(f"{mode}_pre_wns", wns)
            if cells[0] == "final" and wns is not None:
                r[f"{mode}_post_wns"] = wns
            if mode == "hold":
                for name in ("Buffers", "Inserted"):
                    if name in cols:
                        try:
                            b = int(cells[cols.index(name)])
                            r["hold_bufs_peak"] = max(int(r.get("hold_bufs_peak", 0)), b)
                        except (ValueError, IndexError):
                            pass
                        break
        m = _RE_HOLD_INS.search(line)
        if m:
            r["hold_bufs"] = int(m.group(1))
        m = _RE_BUF_INS.search(line)
        if m:
            r["setup_bufs"] = int(r.get("setup_bufs", 0)) + int(m.group(1))
        if "RSZ-0060]" in line:
            r["rsz0060"] = True
    if "hold_bufs" not in r and "hold_bufs_peak" in r:
        r["hold_bufs"] = r["hold_bufs_peak"]
    return r


def msg_counts(text: str) -> Counter:
    c: Counter = Counter()
    for m in _RE_MSG.finditer(text or ""):
        c[f"{m.group(1)[0]} {m.group(2)}"] += 1
    return c


def first_errors(text: str, n: int = 3) -> List[str]:
    out = []
    for line in (text or "").splitlines():
        if _RE_ERRLINE.search(line) and not any(b in line for b in _BENIGN):
            out.append(line.strip()[:300])
            if len(out) >= n:
                break
    return out


def error_code(lines: Iterable[str]) -> Optional[str]:
    for ln in lines:
        m = re.search(r"\[(?:ERROR|FATAL)\s+([A-Z]{2,6}-\d+)\]", ln)
        if m:
            return m.group(1)
    for ln in lines:
        m = re.search(r"\b([A-Z]{2,4}-\d{4})\b", ln)
        if m:
            return m.group(1)
    return None


def parse_grt(text: str) -> Optional[int]:
    tot = None
    in_rep = False
    for line in (text or "").splitlines():
        if "GRT-0096" in line:
            in_rep = True
            continue
        if in_rep:
            m = _RE_GRT_TOT.match(line.strip())
            if m:
                tot, in_rep = int(m.group(3)), False
    return tot


# ---------------------------------------------------------------------------
# per-stage collection
# ---------------------------------------------------------------------------
# §211: ORFS writes the stage report (3_detailed_place.rpt) and metrics json
# BEFORE POST_<STEP>_TCL runs; the JANUS debug dump is written at the end of
# the hook chain. Run 20261104_224244: 3_detailed_place.rpt -3963.6 (pre
# post-DPL repair) vs janus_05_detail_place_paths.rpt -1435.4 (what is in
# 3_place.odb). When the dump is newer than the ORFS report it wins.
POST_HOOK_DUMPS: Dict[str, str] = {
    "3_place": "janus_05_detail_place_paths.rpt",
    "4_cts": "janus_06_cts_paths.rpt",
}
_RE_DUMP = {
    "setup_ws": re.compile(r"^worst slack max\s+(-?[\d.]+)", re.M),
    "hold_ws": re.compile(r"^worst slack min\s+(-?[\d.]+)", re.M),
    "setup_tns": re.compile(r"^tns max\s+(-?[\d.]+)", re.M),
}
_RE_PDR_AFTER = re.compile(r"JANUS_POST_DPL: after .*?slew_viol (-?\d+) cap_viol (-?\d+)")


def _post_hook_override(row: Dict[str, object], key: str, reports_dir: Optional[str],
                        since: Optional[float], log_text: str) -> None:
    name = POST_HOOK_DUMPS.get(key)
    if not name or not reports_dir:
        return
    dp = os.path.join(reports_dir, name)
    if not os.path.isfile(dp) or not _fresh(dp, since):
        return
    dm = os.path.getmtime(dp)
    for rn in _SPEC[key][4]:
        rp = os.path.join(reports_dir, rn)
        if os.path.isfile(rp) and os.path.getmtime(rp) > dm:
            return  # ORFS report is the later state
    t = _read(dp, 8 << 20)
    vals = {k: float(m.group(1)) for k, rx in _RE_DUMP.items() for m in [rx.search(t)] if m}
    if "setup_ws" not in vals:
        return
    row.update(vals)
    row.pop("setup_viol", None)  # pre-hook count; the dump has none
    p = parse_paths(t)
    if p.get("max") or p.get("min"):
        row["paths"] = p
    m = _RE_PDR_AFTER.findall(log_text or "")
    if m:
        row["slew_viol"], row["cap_viol"] = int(m[-1][0]), int(m[-1][1])
    row["timing_src"] = name


# §214: 4_cts aborted before repair_timing (JANUS_CTS_HOLD_ABORT) leaves no
# 4_cts_final.rpt / metrics, so the QoR row was '—/—'. Fill it from the last
# pre-repair timing the hooks printed (JANUS_CTS_LEGALIZE / TAP_SPLIT /
# MACRO_ALIGN '... after setup X hold Y'), else 4_cts_pre_repair.rpt.
PRE_REPAIR_RPT = "4_cts_pre_repair.rpt"
_RE_PRE_AFTER = re.compile(
    r"(JANUS_CTS_[A-Z_]+):.*?\bafter setup\s+(-?[\d.]+)\s+hold\s+(-?[\d.]+)")


def _pre_repair_fill(row: Dict[str, object], reports_dir: Optional[str],
                     since: Optional[float], log_text: str) -> None:
    t = ""
    if reports_dir:
        rp = os.path.join(reports_dir, PRE_REPAIR_RPT)
        if os.path.isfile(rp) and _fresh(rp, since):
            t = _read(rp, 8 << 20)
    hits = _RE_PRE_AFTER.findall(log_text or "")
    if hits:
        tag, s, h = hits[-1]
        row.update(setup_ws=float(s), hold_ws=float(h), pre_repair=True, timing_src=tag)
    elif t:
        vals = {k: float(m.group(1)) for k, rx in _RE_DUMP.items() for m in [rx.search(t)] if m}
        if not ("setup_ws" in vals or "hold_ws" in vals):
            return
        row.update(vals)
        row.update(pre_repair=True, timing_src=PRE_REPAIR_RPT)
    else:
        return
    p = parse_paths(t)
    if p.get("max") or p.get("min"):
        row["paths"] = p


def collect_stage(key: str, logs_dir: Optional[str], reports_dir: Optional[str],
                  metrics: Optional[Dict] = None, since: Optional[float] = None) -> Dict[str, object]:
    """QoR + runtime for one stage. Never raises (missing -> absent key).

    since (§195): ignore logs / reports / metrics json older than this epoch
    (previous run's artifacts of a stage that is re-running or pending)."""
    spec = _SPEC[key]
    row: Dict[str, object] = {"key": key}
    try:
        row.update(stage_runtime(logs_dir, key, since))
        m = metrics if metrics is not None else load_metrics(logs_dir, since)
        mp = spec[3]
        for k, suf in (("setup_ws", "timing__setup__ws"), ("setup_tns", "timing__setup__tns"),
                       ("hold_ws", "timing__hold__ws"), ("hold_tns", "timing__hold__tns"),
                       ("cells", "design__instance__count"),
                       ("area", "design__instance__area"),
                       ("util", "design__instance__utilization"),
                       ("hold_bufs", "design__instance__count__hold_buffer"),
                       ("setup_bufs", "design__instance__count__setup_buffer"),
                       ("skew", "clock__skew__setup"),
                       ("slew_viol", "timing__drv__max_slew"),
                       ("cap_viol", "timing__drv__max_cap"),
                       ("fanout_viol", "timing__drv__max_fanout"),
                       ("setup_viol", "timing__drv__setup_violation_count"),
                       ("hold_viol", "timing__drv__hold_violation_count"),
                       ("drt_viol", "route__drc_errors"),
                       ("antenna", "antenna__violating__nets")):
            v = metric(m, mp, suf)
            if isinstance(v, (int, float)):
                row[k] = v
        rpt_text = ""
        if reports_dir:
            for rn in spec[4]:
                rp = os.path.join(reports_dir, rn)
                if not _fresh(rp, since):
                    continue
                t = _read(rp, 8 << 20)
                if t:
                    rpt_text = t
                    for k, v in parse_stage_rpt(t).items():
                        row.setdefault(k, v)
                    break
        row["paths"] = parse_paths(rpt_text)
        log_text = "\n".join(_read(p) for p in stage_logs(logs_dir, key, since))
        _post_hook_override(row, key, reports_dir, since, log_text)
        rep = parse_repair(log_text)
        for k in ("setup_pre_wns", "setup_post_wns", "hold_pre_wns", "hold_post_wns",
                  "hold_bufs_peak", "rsz0060"):
            if k in rep:
                row[k] = rep[k]
        for k in ("hold_bufs", "setup_bufs"):
            if k in rep and k not in row:
                row[k] = rep[k]
        if key == "5_grt":
            g = parse_grt(log_text)
            if g is not None:
                row["grt_overflow"] = g
        if key == "5_drt":
            v = _RE_DRT.findall(log_text)
            if v:
                row["drt_viol"] = int(v[-1])
                row["drt_iters"] = len(v)
        a = _RE_ANT.findall(log_text)
        if a:
            row.setdefault("antenna", int(a[-1]))
        if key == "4_cts" and row.get("setup_ws") is None and row.get("hold_ws") is None:
            _pre_repair_fill(row, reports_dir, since, log_text)
        if key == "4_cts":
            mo = [ln.strip() for ln in log_text.splitlines() if "JANUS_CTS_MACRO_OFS:" in ln]
            if mo:
                after = [x for x in mo if "after" in x] or mo
                row["macro_ofs"] = after[-1][:200]
                md = _RE_MOFS_D.search(after[-1])
                if md:
                    row["macro_ofs_d"] = float(md.group(1))
        row["msgs"] = msg_counts(log_text).most_common(6)
        row["has_log"] = bool(stage_logs(logs_dir, key, since))
    except Exception as e:  # best-effort
        row["error"] = f"{type(e).__name__}: {e}"
    return row


def _artifact(results_dir: Optional[str], key: str) -> Optional[str]:
    if not results_dir:
        return None
    for a in _SPEC[key][5]:
        p = os.path.join(results_dir, a)
        if os.path.isfile(p):
            return p
    return None


def _log_mtime(logs_dir: Optional[str], key: str) -> Optional[float]:
    ts = []
    for p in stage_logs(logs_dir, key):
        try:
            ts.append(os.path.getmtime(p))
        except OSError:
            pass
    return max(ts) if ts else None


def latest_log_stage(logs_dir: Optional[str]) -> Optional[str]:
    """Stage of the most recently written ORFS log (= failing stage guess)."""
    best, bt = None, -1.0
    for key in STAGE_KEYS:
        t = _log_mtime(logs_dir, key)
        if t is not None and t >= bt:
            best, bt = key, t
    return best


def build_rows(logs_dir: Optional[str], reports_dir: Optional[str],
               results_dir: Optional[str] = None, *, current: Optional[str] = None,
               failed: Optional[str] = None, done: Iterable[str] = (),
               reused: Iterable[str] = (), run_start: Optional[float] = None,
               signoff_dir: Optional[str] = None) -> List[Dict[str, object]]:
    """One row per stage with status DONE / RUNNING / FAILED / REUSED / PENDING."""
    metrics = load_metrics(logs_dir)
    fresh_metrics = load_metrics(logs_dir, run_start) if run_start else metrics
    reused, done = set(reused or ()), set(done or ())
    rows = []
    # §208: stages after the failed / running one are stale leftovers of an
    # older run, never "reused" (run 20261104_172222 failed at 3_place yet
    # mailed the old 4_cts "reused" row and a RED 6191-hold-buffer flag).
    barrier = next((i for i, k in enumerate(STAGE_KEYS) if k in (failed, current)), None)
    # §210: with nothing running/failed (stage email after a --stop-flow-at
    # stop) the barrier is the last stage done / logged in THIS run: make
    # always reruns downstream, so a later stage can never be "reused" (run
    # 20261104_201235 stopped after 3_place, mailed old 4_cts + RED 6191 hbuf).
    if barrier is None and run_start:
        now = [i for i, k in enumerate(STAGE_KEYS)
               if k in done or ((lambda t: bool(t and t >= run_start))(_log_mtime(logs_dir, k)))]
        barrier = max(now) if now else None
    for idx, key in enumerate(STAGE_KEYS):
        if barrier is not None and idx > barrier:
            r = collect_stage(key, logs_dir, reports_dir, {}, since=float("inf"))
            r["status"] = "PENDING"
            r["_ran_now"] = False
            rows.append(r)
            continue
        art = _artifact(results_dir, key)
        lt = _log_mtime(logs_dir, key)
        ran_now = bool(run_start and lt and lt >= run_start)
        is_reused = key in reused or bool((art or key in done) and run_start and not ran_now
                                         and key not in done and key != current and key != failed)
        # §195: a stage re-running / pending / failed in THIS run must not show the
        # previous run's logs, reports or metrics (run 20261102_221045 mailed the
        # old 4_cts hold -1417 / 14470 hbuf KILL while 4_cts had just started).
        if run_start and not is_reused:
            r = collect_stage(key, logs_dir, reports_dir, fresh_metrics, since=run_start)
        else:
            r = collect_stage(key, logs_dir, reports_dir, metrics)
        if key == failed:
            st = "FAILED"
        elif key == current:
            st = "RUNNING"
        elif key in reused or ((art or key in done) and run_start and not ran_now
                               and key not in done):
            st = "REUSED"
        elif art or key in done:
            st = "DONE"
        else:
            st = "PENDING"
        r["status"] = st
        r["_ran_now"] = ran_now
        rows.append(r)
    # A stage with logs that precedes an active/finished stage completed even
    # when its result artifact is not visible (e.g. results dir unknown).
    active = [i for i, r in enumerate(rows) if r["status"] != "PENDING"]
    last = max(active) if active else -1
    for r in rows[:last]:
        if r["status"] == "PENDING" and r.get("has_log"):
            r["status"] = "REUSED" if (run_start and not r["_ran_now"]) else "DONE"
    for r in rows:
        r.pop("_ran_now", None)
    if signoff_dir and os.path.isdir(signoff_dir):
        r = {"key": "signoff", "status": "DONE" if glob.glob(os.path.join(signoff_dir, "*"))
             else "PENDING", "wall": None, "cpu": None, "peak_kb": None}
        txt = "\n".join(_read(p, 1 << 20) for p in sorted(glob.glob(os.path.join(signoff_dir, "*.log"))))
        el = parse_elapsed(txt)
        if el:
            r.update(wall=sum(e["wall"] for e in el), cpu=sum(e["cpu"] for e in el),
                     peak_kb=max(e["peak_kb"] for e in el))
        rows.append(r)
    return rows


# ---------------------------------------------------------------------------
# verdicts
# ---------------------------------------------------------------------------
def red_flags(rows: List[Dict[str, object]]) -> List[Tuple[str, str]]:
    """(level, text). level KILL means the user's kill rule fired."""
    out: List[Tuple[str, str]] = []
    for r in rows:
        if r.get("status") == "PENDING":
            continue
        k = r.get("key")
        hb = r.get("hold_bufs_peak") or r.get("hold_bufs")
        if isinstance(hb, (int, float)) and hb > HOLD_BUF_KILL:
            out.append(("KILL", f"{k}: {int(hb)} hold buffers > {HOLD_BUF_KILL} -> KILL RUN"))
        elif isinstance(hb, (int, float)) and hb > HOLD_BUF_WARN:
            out.append(("RED", f"{k}: {int(hb)} hold buffers > {HOLD_BUF_WARN}"))
        hp = r.get("hold_pre_wns")
        if isinstance(hp, (int, float)) and hp < HOLD_PRE_KILL_PS:
            out.append(("RED", f"{k}: pre-repair hold WNS {hp:.0f} ps < {HOLD_PRE_KILL_PS:.0f} ps"))
        hw = r.get("hold_ws")
        if (k == "4_cts" and r.get("status") in ("DONE", "FAILED") and not r.get("pre_repair")
                and isinstance(hw, (int, float)) and hw < 0):
            out.append(("KILL", f"{k}: hold WNS {hw:.1f} ps after repair (not closed) -> KILL RUN"))
        if r.get("rsz0060"):
            out.append(("RED", f"{k}: RSZ-0060 buffer cap hit during repair_timing"))
        g = r.get("grt_overflow")
        if isinstance(g, int) and g > 0:
            out.append(("RED", f"{k}: GRT overflow {g}"))
        d = r.get("drt_viol")
        if k == "5_drt" and r.get("status") in ("DONE", "FAILED") and isinstance(d, (int, float)) and d > 0:
            out.append(("RED", f"{k}: {int(d)} DRT violations after final iteration"))
    return out


def _last_qor_row(rows: List[Dict[str, object]], upto: Optional[str] = None):
    best = None
    for r in rows:
        if r.get("status") in ("DONE", "REUSED", "RUNNING", "FAILED") and r.get("setup_ws") is not None:
            best = r
        if upto and r.get("key") == upto:
            break
    return best


def key_qor(r: Dict[str, object]) -> str:
    """Compact one-cell QoR summary for the main table."""
    k = r.get("key")
    parts = []
    if r.get("setup_ws") is not None:
        parts.append(f"S {fmt_ps(r['setup_ws'])}")
    if r.get("hold_ws") is not None:
        parts.append(f"H {fmt_ps(r['hold_ws'])}")
    if k == "1_synth" and r.get("cells") is not None:
        parts.append(f"{fmt_int(r['cells'])} cells")
    if k == "4_cts" and r.get("hold_bufs") is not None:
        parts.append(f"hbuf {fmt_int(r['hold_bufs'])}")
    if k == "5_grt" and r.get("grt_overflow") is not None:
        parts.append(f"ovfl {r['grt_overflow']}")
    if k == "5_drt" and r.get("drt_viol") is not None:
        parts.append(f"DRC {fmt_int(r['drt_viol'])}")
    if r.get("pre_repair"):
        parts.append("pre-repair")
    return " · ".join(parts) or DASH


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------
_MAIN_COLS = ("Stage", "Status", "Wall", "CPU", "Mem", "Key QoR (ps)")
_DET_COLS = ("Stage", "Setup WNS/TNS #", "Hold WNS/TNS #", "Cells·Util", "Bufs H/S", "Notes")
_ST_SHORT = {"DONE": "done", "RUNNING": "running", "FAILED": "FAILED",
             "REUSED": "reused", "PENDING": "·"}


def _main_cells(r) -> List[str]:
    if r.get("_post"):   # §214
        return [str(r.get("key")), _post_status(r), fmt_dur(r.get("wall")),
                fmt_dur(r.get("cpu")), fmt_mem(r.get("peak_kb")), r.get("post_note") or DASH]
    if r.get("status") == "PENDING":   # logs on disk (if any) are from an older run
        return [str(r.get("key")), _ST_SHORT["PENDING"], DASH, DASH, DASH, DASH]
    return [str(r.get("key")), _ST_SHORT.get(str(r.get("status")), str(r.get("status"))),
            fmt_dur(r.get("wall")), fmt_dur(r.get("cpu")), fmt_mem(r.get("peak_kb")),
            key_qor(r)]


def _det_cells(r) -> List[str]:
    def wt(w, t, n):
        return f"{fmt_ps(w)}/{fmt_ps(t)} {fmt_int(n) if n is not None else ''}".strip()
    if r.get("_post"):   # §214
        return [str(r.get("key")), DASH, DASH, DASH, DASH,
                r.get("post_note") or _post_status(r)]
    util = r.get("util")
    cu = f"{fmt_int(r.get('cells'))}·{(f'{float(util) * 100:.0f}%' if isinstance(util, (int, float)) and util <= 1.5 else fmt_int(util) if util is not None else DASH)}"
    notes = []
    if r.get("pre_repair"):
        notes.append(f"pre-repair ({r.get('timing_src') or '?'})")
    if r.get("hold_pre_wns") is not None:
        notes.append(f"pre-hold {fmt_ps(r['hold_pre_wns'])}")
    if r.get("setup_pre_wns") is not None:
        notes.append(f"pre-setup {fmt_ps(r['setup_pre_wns'])}")
    if r.get("skew") is not None:
        notes.append(f"skew {fmt_ps(r['skew'], False)}")
    if r.get("macro_ofs_d") is not None:
        notes.append(f"D {r['macro_ofs_d']:.0f}")
    if r.get("grt_overflow") is not None:
        notes.append(f"ovfl {r['grt_overflow']}")
    if r.get("drt_viol") is not None:
        notes.append(f"DRC {fmt_int(r['drt_viol'])}" + (f"@{r['drt_iters']}it" if r.get("drt_iters") else ""))
    if r.get("antenna"):
        notes.append(f"ant {fmt_int(r['antenna'])}")
    drv = [f"{n} {fmt_int(r[k])}" for k, n in (("slew_viol", "slew"), ("cap_viol", "cap"),
                                                ("fanout_viol", "fo")) if r.get(k)]
    notes += drv
    return [str(r.get("key")), wt(r.get("setup_ws"), r.get("setup_tns"), r.get("setup_viol")),
            wt(r.get("hold_ws"), r.get("hold_tns"), r.get("hold_viol")), cu,
            f"{fmt_int(r.get('hold_bufs'))}/{fmt_int(r.get('setup_bufs'))}",
            ", ".join(notes) or DASH]


# ---------------------------------------------------------------------------
# §214: post-custom-script rows ('4_cts+post (soc_cts_probe)')
# ---------------------------------------------------------------------------
# A post script is described by a dict (the agent writes the same dict to
# <run_dir>/post_script/<block>/post_script.json):
#   script  path            name    label (default: script basename w/o ext)
#   after   STAGES key it follows (None -> failed / stopped / last stage)
#   status  DONE | FAILED | SKIPPED          reason  why skipped / failed
#   rc, wall, cpu, peak_kb (measured)        log, out_dir   for notes
#   notes   precomputed note (else derived from log / out_dir)
_POST_OWN = ("post_custom_script.log", "post_script.json")
_RE_POST_SUMMARY = re.compile(r"\b(SUMMARY|RESULT|VERDICT)\b", re.I)


def post_script_name(ps: Dict[str, object]) -> str:
    n = ps.get("name") or os.path.splitext(os.path.basename(str(ps.get("script") or "post")))[0]
    return str(n) or "post"


def post_script_notes(log: Optional[str], out_dir: Optional[str],
                      rc: Optional[int] = None) -> str:
    """Short note: first summary-ish line of the script log (or its first
    error when rc != 0) + the report(s) it wrote to JANUS_POST_SCRIPT_OUT."""
    parts: List[str] = []
    lines = [ln.strip() for ln in _read(log, 4 << 20).splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    pick = None
    if rc not in (None, 0):
        pick = next((ln for ln in lines if _RE_ERRLINE.search(ln)), None)
    if pick is None:
        pick = next((ln for ln in lines if _RE_POST_SUMMARY.search(ln)), None)
    if pick:
        parts.append(pick[:100])
    outs: List[str] = []
    if out_dir and os.path.isdir(out_dir):
        for root, _d, files in os.walk(out_dir):
            for fn in sorted(files):
                if fn not in _POST_OWN:
                    outs.append(os.path.relpath(os.path.join(root, fn), out_dir))
    if outs:
        parts.append("out: " + outs[0] + (f" (+{len(outs) - 1})" if len(outs) > 1 else ""))
    elif log and os.path.isfile(log):
        parts.append("log: post_script/" + os.path.basename(log))
    return " · ".join(parts)


def _post_row(ps: Dict[str, object], after: str) -> Dict[str, object]:
    st = str(ps.get("status") or "SKIPPED").upper()
    r: Dict[str, object] = {"key": f"{after}+post ({post_script_name(ps)})", "_post": True,
                            "after": after, "status": st, "rc": ps.get("rc"),
                            "reason": ps.get("reason") or "",
                            "wall": ps.get("wall"), "cpu": ps.get("cpu"),
                            "peak_kb": ps.get("peak_kb")}
    log = ps.get("log")
    if st != "SKIPPED" and log and r["cpu"] is None:
        el = parse_elapsed(_read_tail_text(str(log))) if os.path.isfile(str(log)) else []
        if el:   # ORFS/GNU-time banner(s) inside the script log
            r["cpu"] = sum(e["cpu"] for e in el)
            r["peak_kb"] = r["peak_kb"] or max(e["peak_kb"] for e in el)
            r["wall"] = r["wall"] if r["wall"] is not None else sum(e["wall"] for e in el)
    note = ps.get("notes")
    if note is None and st != "SKIPPED":
        note = post_script_notes(log, ps.get("out_dir"), ps.get("rc"))
    r["post_note"] = str(note or "")
    return r


def merge_post_rows(rows: List[Dict[str, object]], post_scripts: Optional[Iterable[Dict]],
                    default_after: Optional[str] = None) -> List[Dict[str, object]]:
    """Insert one row per post script right after the stage it follows
    (several scripts on one stage keep their order). No scripts -> *rows*
    returned unchanged."""
    pss = [p for p in (post_scripts or []) if isinstance(p, dict)]
    if not pss:
        return rows
    keys = [r.get("key") for r in rows]
    active = [r.get("key") for r in rows if r.get("key") in STAGE_KEYS
              and r.get("status") != "PENDING"]
    fallback = default_after if default_after in keys else (active[-1] if active else None)
    out = list(rows)
    for ps in pss:
        after = ps.get("after") if ps.get("after") in keys else fallback
        row = _post_row(ps, after or "run")
        if after is None:
            idx = next((i for i, r in enumerate(out) if r.get("key") == "signoff"), len(out))
        else:
            idx = next(i for i, r in enumerate(out) if r.get("key") == after) + 1
            while idx < len(out) and out[idx].get("_post"):
                idx += 1
        out.insert(idx, row)
    return out


def _post_status(r) -> str:
    st = str(r.get("status"))
    if st == "SKIPPED":
        return f"skipped ({r['reason']})" if r.get("reason") else "skipped"
    if st == "FAILED":
        why = r.get("reason") or (f"rc {r['rc']}" if r.get("rc") not in (None, 0) else "")
        return f"FAILED ({why})" if why else "FAILED"
    return _ST_SHORT.get(st, st.lower())


def _txt_table(head: Sequence[str], body: List[List[str]]) -> str:
    w = [max(len(x[i]) for x in [list(head)] + body) for i in range(len(head))]
    ln = lambda c: "  ".join(c[i].ljust(w[i]) for i in range(len(c))).rstrip()  # noqa: E731
    return "\n".join([ln(list(head)), ln(["-" * n for n in w])] + [ln(c) for c in body])


_TD = "border:1px solid #bbb;padding:2px 4px;"
_STC = {"DONE": "#d4edda", "FAILED": "#f8d7da", "RUNNING": "#cce5ff", "REUSED": "#e2e3e5",
        "SKIPPED": "#fff3cd"}


def _html_table(head: Sequence[str], rows_cells: List[List[str]], statuses: List[str],
                status_col: Optional[int] = 1) -> str:
    th = "".join(f'<th style="{_TD}background:#eee;text-align:left">{_h.escape(h)}</th>' for h in head)
    trs = []
    for cells, st in zip(rows_cells, statuses):
        tds = []
        for i, v in enumerate(cells):
            sty = _TD
            if status_col is not None and i == status_col and st in _STC:
                sty += f"background:{_STC[st]};"
            if re.search(r"(^|\s|/)-\d", v):
                sty += "color:#b00;"
            tds.append(f'<td style="{sty}">{_h.escape(v)}</td>')
        trs.append(f"<tr>{''.join(tds)}</tr>")
    return ('<table style="border-collapse:collapse;font-family:monospace;font-size:12px">'
            f"<tr>{th}</tr>{''.join(trs)}</table>")


def subject_line(block: str, kind: str, stage: Optional[str], rows: List[Dict[str, object]],
                 flags: List[Tuple[str, str]], code: Optional[str] = None,
                 elapsed: Optional[float] = None) -> str:
    n_done = sum(1 for r in rows if r.get("key") in STAGE_KEYS
                 and r.get("status") in ("DONE", "REUSED"))
    total = len(STAGE_KEYS)
    kill = any(lv == "KILL" for lv, _ in flags)
    if kind == "failed":
        s = f"[JANUS] {block} FAILED at {stage or '?'}"
        if code:
            s += f" ({code})"
    else:
        tag = "DONE" if kind == "final" else f"{stage} DONE"
        s = f"[JANUS] {block} {tag}"
        q = _last_qor_row(rows, None if kind == "final" else stage)
        if q:
            s += f" · WNS {fmt_ps(q.get('setup_ws'))}ps"
            if q.get("hold_ws") is not None:
                s += f" · hold {fmt_ps(q.get('hold_ws'))}ps"
        s += f" · {n_done}/{total}"
        if elapsed:
            s += f" · {fmt_dur(elapsed)}"
    if kill:
        s += " · !! KILL RUN?"
    elif any(lv == "RED" for lv, _ in flags):
        s += " · RED FLAG"
    return s


def _ctx_lines(ctx: Dict[str, object]) -> List[str]:
    out = []
    for k, lab in (("run_id", "Run"), ("git", "Git"), ("host", "Host"), ("cores", "Cores"),
                   ("resume", "Resume"), ("prev_run", "Last DONE"), ("cmd", "Cmd")):
        v = ctx.get(k)
        if v not in (None, ""):
            out.append(f"{lab:<9}: {v}")
    return out


def build_report(block: str, kind: str, *, stage: Optional[str] = None,
                 logs_dir: Optional[str] = None, reports_dir: Optional[str] = None,
                 results_dir: Optional[str] = None, signoff_dir: Optional[str] = None,
                 agent_log: Optional[str] = None, current: Optional[str] = None,
                 done: Iterable[str] = (), reused: Iterable[str] = (),
                 run_start: Optional[float] = None, elapsed: Optional[float] = None,
                 ctx: Optional[Dict[str, object]] = None,
                 tail_lines: Optional[int] = None,
                 drive_note: Optional[str] = None,
                 post_scripts: Optional[Iterable[Dict]] = None) -> Dict[str, object]:
    """kind: 'stage' (stage just completed) | 'final' (block DONE) | 'failed'.

    §214: *post_scripts* (see merge_post_rows) add one '<stage>+post (<name>)'
    row per --post-custom-script right after the stage it follows, in both
    the stage table and the QoR detail table.

    Returns {subject, text, html, rows, flags, failed_stage}. Never raises:
    on an internal error a minimal report naming the error is returned.
    """
    ctx = dict(ctx or {})
    try:
        tail_n = tail_lines if tail_lines else tail_lines_setting()
        failed = None
        if kind == "failed":
            failed = stage if stage in STAGE_KEYS else latest_log_stage(logs_dir)
            if failed and stage not in STAGE_KEYS and run_start:
                lt = _log_mtime(logs_dir, failed)
                if lt is not None and lt < run_start:
                    failed = None   # died before ORFS wrote a log this run
            failed = failed or stage
        rows = build_rows(logs_dir, reports_dir, results_dir, current=current,
                          failed=failed, done=set(done or ()) | ({stage} if kind == "stage" and stage else set()),
                          reused=reused, run_start=run_start, signoff_dir=signoff_dir)
        flags = red_flags(rows)
        rows = merge_post_rows(rows, post_scripts,
                               failed if kind == "failed" else stage if kind == "stage" else None)
        # failure details
        errs: List[str] = []
        tail: List[str] = []
        tail_src = None
        if kind == "failed":
            logs = stage_logs(logs_dir, failed) if failed in STAGE_KEYS else []
            if logs:
                tail_src = max(logs, key=lambda p: os.path.getmtime(p))
            elif agent_log and os.path.isfile(agent_log):
                tail_src = agent_log
            for src in ([tail_src] if tail_src else []) + ([agent_log] if agent_log and agent_log != tail_src else []):
                errs = first_errors(_read(src))
                if errs:
                    break
            tail = read_tail(tail_src, tail_n) if tail_src else []
        code = error_code(errs) if errs else None
        subject = subject_line(block, kind, failed if kind == "failed" else stage, rows,
                               flags, code, elapsed)

        main_body = [_main_cells(r) for r in rows]
        det_rows = [r for r in rows if r.get("_post") or (r.get("key") in STAGE_KEYS
                                                           and r.get("status") != "PENDING")]
        det_body = [_det_cells(r) for r in det_rows]
        focus_key = failed or (stage if kind == "stage" else None)
        if focus_key is None:
            q = _last_qor_row(rows)
            focus_key = q.get("key") if q else None
        focus = next((r for r in rows if r.get("key") == focus_key), None)

        # delta vs previous stage with timing
        delta = ""
        if focus and focus.get("setup_ws") is not None:
            prev = None
            for r in rows:
                if r is focus:
                    break
                if r.get("setup_ws") is not None and r.get("status") in ("DONE", "REUSED"):
                    prev = r
            if prev:
                ds = float(focus["setup_ws"]) - float(prev["setup_ws"])
                delta = f"vs {prev['key']}: setup {ds:+.1f} ps"
                if focus.get("hold_ws") is not None and prev.get("hold_ws") is not None:
                    delta += f", hold {float(focus['hold_ws']) - float(prev['hold_ws']):+.1f} ps"

        T: List[str] = [subject, ""]
        if flags:
            T.append("RED FLAGS")
            T += [f"  [{lv}] {t}" for lv, t in flags]
            T.append("")
        if kind == "failed":
            T.append(f"FAILED STAGE: {failed or '?'}")
            T.append("First error(s):")
            T += [f"  {e}" for e in errs] or ["  (no ERROR line found in the stage log)"]
            T.append("")
        T += ["STAGES", _txt_table(_MAIN_COLS, main_body), ""]
        if det_body:
            T += ["QoR DETAIL (ps; '#'=violating endpoints)", _txt_table(_DET_COLS, det_body), ""]
        if delta:
            T += [f"Delta {delta}", ""]
        P: List[str] = []
        if focus:
            pp = focus.get("paths") or {}
            for typ, lab in (("max", "setup"), ("min", "hold")):
                if pp.get(typ):
                    P.append(f"Worst {lab} paths @ {focus['key']}:")
                    P += [f"  {sl:>9.1f}  {a} -> {b}" for a, b, sl in pp[typ]]
            if focus.get("msgs"):
                P.append(f"Messages @ {focus['key']}: "
                         + ", ".join(f"{c} x{n}" for c, n in focus["msgs"]))
            if focus.get("macro_ofs"):
                P.append(focus["macro_ofs"])
        if P:
            T += P + [""]
        if drive_note:
            T += [drive_note, ""]
        cl = _ctx_lines(ctx)
        if cl:
            T += ["RUN"] + cl + [""]
        if kind == "failed":
            T.append(f"LAST {len(tail)} LINES of {os.path.basename(tail_src) if tail_src else '(no log)'}:")
            T += tail or ["(log not found)"]
        text = "\n".join(T).rstrip() + "\n"

        # ---- HTML ----
        H: List[str] = ['<div style="font-family:Arial,sans-serif;font-size:13px">',
                        f"<p><b>{_h.escape(subject)}</b></p>"]
        if flags:
            H.append('<p style="background:#f8d7da;padding:4px">'
                     + "<br>".join(f"<b>[{lv}]</b> {_h.escape(t)}" for lv, t in flags) + "</p>")
        if kind == "failed":
            H.append(f"<p><b>Failed stage:</b> {_h.escape(str(failed or '?'))}<br><b>First error(s):</b></p>"
                     '<pre style="font-size:11px;white-space:pre-wrap;color:#b00">'
                     + _h.escape("\n".join(errs) or "(no ERROR line found)") + "</pre>")
        H.append(_html_table(_MAIN_COLS, main_body, [str(r.get("status")) for r in rows]))
        if det_body:
            H.append("<p><b>QoR detail</b> (ps; # = violating endpoints)</p>")
            H.append(_html_table(_DET_COLS, det_body, [str(r.get("status")) for r in det_rows],
                                 status_col=None))
        if delta:
            H.append(f"<p>Delta {_h.escape(delta)}</p>")
        if P:
            H.append('<pre style="font-size:11px;white-space:pre-wrap">' + _h.escape("\n".join(P)) + "</pre>")
        if drive_note:
            H.append("<p>" + "<br>".join(_linkify(x) for x in drive_note.splitlines()) + "</p>")
        if cl:
            H.append('<pre style="font-size:11px;white-space:pre-wrap;color:#555">'
                     + _h.escape("\n".join(cl)) + "</pre>")
        if kind == "failed":
            H.append(f"<p><b>Last {len(tail)} lines</b> of "
                     f"{_h.escape(os.path.basename(tail_src) if tail_src else '(no log)')}</p>"
                     '<pre style="font-size:10px;white-space:pre-wrap;background:#f6f6f6">'
                     + _h.escape("\n".join(tail) or "(log not found)") + "</pre>")
        H.append("</div>")
        return {"subject": subject, "text": text, "html": "".join(H), "rows": rows,
                "flags": flags, "failed_stage": failed, "errors": errs, "tail": tail}
    except Exception as e:  # never crash the flow
        subj = f"[JANUS] {block} {'FAILED' if kind == 'failed' else (stage or '') + ' DONE'}".replace("  ", " ")
        msg = f"{subj}\n\n(stage report unavailable: {type(e).__name__}: {e})\n"
        return {"subject": subj, "text": msg, "html": "<pre>" + _h.escape(msg) + "</pre>",
                "rows": [], "flags": [], "failed_stage": stage, "errors": [], "tail": [],
                "error": str(e)}


def _linkify(line: str) -> str:
    esc = _h.escape(line)
    return re.sub(r"(https?://[^\s<]+)", r'<a href="\1">\1</a>', esc)


def prev_done_summary(reports_dir: Optional[str]) -> str:
    """'setup WS x hold WS y (<generated>)' from the last qor.json if that run
    was DONE (written by notify() at the previous settle); else ''."""
    try:
        with open(os.path.join(reports_dir or "", "qor.json")) as fh:
            q = json.load(fh)
        if str(q.get("status", "")).upper() != "DONE":
            return ""
        t = q.get("timing") or {}
        return (f"setup WS {fmt_ps(t.get('setup_worst_slack'))} ps, hold WS "
                f"{fmt_ps(t.get('hold_worst_slack'))} ps ({q.get('generated', '?')})")
    except Exception:
        return ""
