"""§247 post-route ECO rows for the stage-table email and the report bundle.

An adopted post-route ECO (janus_agent._eco_pass_orfs) replaces 6_final.* but
not 6_finish.rpt, and a DISCARDED pass leaves no trace in the reports dir.
soc_top 20261115_211511 (--eco-hold-only, margin 25) mailed only 1_synth..6_finish
(6_finish -66.0 / -171, 422 violators), no row for the in-force ECO
(-68.67 / -7.31, DRC 0) nor for the discarded attempt, a stale "5_drt: 1 DRT
violations" RED flag and "violators n/a".

Sources (all best-effort, pure functions, never raise to the caller):
  * reports/janus_eco_adopted.json   the ECO in force (agent, §150)
  * reports/janus_eco_history.json   one entry per ECO pass, adopt AND discard
                                     (agent, §247; persists across runs)
  * logs/janus_eco_<mode>.log        latest pass (fallback for passes recorded
                                     before §247 / truncated bundle logs)
  * reports/janus_eco_<mode>.rpt     violator counts for older adopted JSONs
  * the agent block log              '[janus-eco] HH:MM:SS <mode> ECO DISCARDED — why'
Everything older than 6_finish.rpt belongs to an earlier build and is ignored.
"""

from __future__ import annotations

import glob
import json
import os
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple

ADOPTED_JSON = "janus_eco_adopted.json"
HISTORY_JSON = "janus_eco_history.json"
HISTORY_CAP = 50
_HEAD = 256 << 10
_TAIL = 4 << 20

_RE_PR = re.compile(r"^JANUS_PR(_PRE)?\s+(setup|hold)\s+(wns|tns|viol)\s+(-?\d+(?:\.\d+)?)\s*$", re.M)
_RE_DRC = re.compile(r"^JANUS_PR\s+drc\s+(-?\d+)", re.M)
_RE_ERR = re.compile(r"^JANUS_PR_ERROR\s+(.+)$", re.M)
_RE_BEGIN = re.compile(r"^JANUS_PR_BEGIN\s+(\w+)", re.M)
_RE_MARGIN = re.compile(r"^JANUS_PR\s+(?:hold_margin|hold_guard margin)\s+(-?[\d.]+)"
                        r"|^JANUS_PR hold_pad start .*?\btarget\s+(-?[\d.]+)", re.M)
_RE_HBUF = re.compile(r"RSZ-0032\]\s+Inserted\s+(\d+)\s+hold buffers")
_RE_OPEN = re.compile(r"checkConnectivity break")
_RE_BANNER = re.compile(r"Elapsed time:\s*([\d:.]+)\s*\[h:\]min:sec")
_RE_AGENT = re.compile(r"^\[janus-eco\]\s+(\d\d:\d\d:\d\d)\s+(\w+) ECO (ADOPTED|DISCARDED|via ORFS make run)"
                       r"(?:\s+—\s+(.*)|\s+\(hold margin\s+(-?[\d.]+)\s*ps)?", re.M)


def _mtime(p: Optional[str]) -> Optional[float]:
    try:
        return os.path.getmtime(p) if p else None
    except OSError:
        return None


def _read_ends(path: Optional[str], head: int = _HEAD, tail: int = _TAIL) -> str:
    """Head + tail of a (possibly 100 MB) log: PRE lines sit at the top,
    POST/ERROR lines at the bottom."""
    if not path:
        return ""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            n = fh.tell()
            fh.seek(0)
            if n <= head + tail:
                return fh.read().decode("utf-8", "replace")
            a = fh.read(head)
            fh.seek(n - tail)
            return (a + b"\n" + fh.read()).decode("utf-8", "replace")
    except OSError:
        return ""


def _wall(s: str) -> Optional[float]:
    try:
        sec = 0.0
        for p in s.strip().split(":"):
            sec = sec * 60 + float(p)
        return sec
    except ValueError:
        return None


def parse_eco_log(path: Optional[str]) -> Dict[str, object]:
    """Numbers of one post_route_opt.tcl pass (logs/janus_eco_<mode>.log)."""
    txt = _read_ends(path)
    out: Dict[str, object] = {"pre": {}, "post": {}, "mode": None, "drc": None, "error": None,
                              "margin": None, "hold_bufs": None, "open_nets": 0,
                              "wrote": False, "ended": False, "wall_s": None,
                              "truncated": "[JANUS-BUNDLE] truncated" in txt[:4096]}
    if not txt:
        return out
    for pre, kind, met, val in _RE_PR.findall(txt):
        d = out["pre" if pre else "post"]
        d[f"{kind}_{'ws' if met == 'wns' else met}"] = int(val) if met == "viol" else float(val)
    m = _RE_DRC.findall(txt)
    out["drc"] = int(m[-1]) if m else None
    m = _RE_ERR.findall(txt)
    out["error"] = m[-1].strip() if m else None
    m = _RE_BEGIN.search(txt)
    out["mode"] = m.group(1) if m else None
    m = _RE_MARGIN.findall(txt)
    if m:
        out["margin"] = float(m[0][0] or m[0][1])
    hb = [int(x) for x in _RE_HBUF.findall(txt)]
    out["hold_bufs"] = sum(hb) if hb else None
    out["open_nets"] = len(_RE_OPEN.findall(txt))
    out["wrote"] = bool(re.search(r"^JANUS_PR wrote ", txt, re.M))
    out["ended"] = bool(re.search(r"^JANUS_PR_END\b", txt, re.M))
    w = [_wall(x) for x in _RE_BANNER.findall(txt)]
    w = [x for x in w if x is not None]
    out["wall_s"] = sum(w) if w else None
    return out


def short_error(err: Optional[str], open_nets: int = 0) -> str:
    if not err:
        return ""
    s = err.strip()
    if "DRT-0206" in s and open_nets:
        s += f" ({open_nets} open nets after incremental DRT; no post-ECO RCX)"
    return s


def count_rpt_violators(path: Optional[str]) -> Tuple[Optional[int], Optional[int]]:
    """(setup, hold) violating endpoints from janus_eco_<mode>.rpt (report_checks
    -group_path_count 10 min / 5 max). Exact only while every group shows fewer
    violators than its path cap; otherwise None (a lower bound is not a count)."""
    txt = _read_ends(path, 8 << 20, 8 << 20)
    if not txt:
        return None, None
    counts: Dict[str, Dict[str, List[int]]] = {"min": {}, "max": {}}
    grp, typ = None, None
    for ln in txt.splitlines():
        s = ln.strip()
        if s.startswith("Path Group:"):
            grp = s.split(":", 1)[1].strip()
        elif s.startswith("Path Type:"):
            typ = s.split(":", 1)[1].strip()
            if typ in counts and grp is not None:
                counts[typ].setdefault(grp, [0, 0])[0] += 1
        elif s.endswith("slack (VIOLATED)") and typ in counts and grp is not None:
            counts[typ][grp][1] += 1
    res = []
    for typ, cap in (("max", 5), ("min", 10)):
        g = counts[typ]
        if not g:
            res.append(None)
            continue
        exact = all(v < cap or v < shown for shown, v in g.values())
        res.append(sum(v for _s, v in g.values()) if exact else None)
    return res[0], res[1]


# ---------------------------------------------------------------------------
# history (written by the agent on every ECO pass)
# ---------------------------------------------------------------------------
def read_history(reports_dir: Optional[str]) -> List[Dict[str, object]]:
    try:
        with open(os.path.join(reports_dir or "#", HISTORY_JSON)) as fh:
            d = json.load(fh)
        return [e for e in d if isinstance(e, dict)] if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def append_history(reports_dir: Optional[str], entry: Dict[str, object]) -> bool:
    if not reports_dir or not os.path.isdir(reports_dir):
        return False
    hist = read_history(reports_dir) + [entry]
    p = os.path.join(reports_dir, HISTORY_JSON)
    try:
        with open(p + ".tmp", "w") as fh:
            json.dump(hist[-HISTORY_CAP:], fh, indent=2)
        os.replace(p + ".tmp", p)
        return True
    except OSError:
        return False


def read_adopted(reports_dir: Optional[str]) -> Optional[Dict[str, object]]:
    """janus_eco_adopted.json if in force (not older than 6_finish.rpt)."""
    p = os.path.join(reports_dir or "#", ADOPTED_JSON)
    fin = _mtime(os.path.join(reports_dir or "#", "6_finish.rpt"))
    pt = _mtime(p)
    if pt is None or (fin is not None and fin > pt):
        return None
    try:
        with open(p) as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or (d.get("setup_ws") is None and d.get("hold_ws") is None):
        return None
    d["_mtime"] = pt
    return d


def _same(a, b) -> bool:
    try:
        return abs(float(a) - float(b)) < 1e-3
    except (TypeError, ValueError):
        return False


def _agent_verdicts(agent_log: Optional[str]) -> List[Dict[str, object]]:
    out: List[Dict[str, object]] = []
    for hh, mode, what, why, margin in _RE_AGENT.findall(_read_ends(agent_log, 1 << 20, 8 << 20)):
        out.append({"hh": hh, "mode": mode, "what": what, "why": why or "",
                    "margin": float(margin) if margin else None})
    return out


def _dur_hhmmss(a: str, b: str) -> Optional[float]:
    try:
        t0 = datetime.strptime(a, "%H:%M:%S")
        t1 = datetime.strptime(b, "%H:%M:%S")
    except ValueError:
        return None
    d = (t1 - t0).total_seconds()
    return d + 86400 if d < 0 else d


def rpt_from_adopted(reports_dir: Optional[str], rpt: str,
                     adopted: Optional[Dict[str, object]] = None) -> bool:
    """True when janus_eco_<mode>.rpt was written by the ADOPTED pass: not
    newer than janus_eco_adopted.json (a later completed-but-discarded pass
    rewrites it) and not older than 6_finish.rpt."""
    a = adopted if adopted is not None else read_adopted(reports_dir)
    rt = _mtime(rpt)
    fin = _mtime(os.path.join(reports_dir or "#", "6_finish.rpt"))
    return bool(a and rt is not None and rt <= float(a.get("_mtime") or 0) + 5
                and (fin is None or rt >= fin))


def adopted_violators(reports_dir: Optional[str], logs_dir: Optional[str],
                      adopted: Optional[Dict[str, object]] = None) -> Tuple[Optional[int], Optional[int]]:
    """(setup_viol, hold_viol) of the ECO in force. JSON (§247) -> history /
    ECO log whose post numbers match -> janus_eco_<mode>.rpt written before the
    adoption -> setup WNS == TNS < 0 means exactly one setup violator."""
    a = adopted if adopted is not None else read_adopted(reports_dir)
    if not a:
        return None, None
    sv, hv = a.get("setup_viol"), a.get("hold_viol")
    mode = a.get("mode") or "hold"

    def _match(post: Dict[str, object]) -> bool:
        return _same(post.get("setup_ws"), a.get("setup_ws")) and _same(post.get("hold_ws"), a.get("hold_ws"))
    for e in reversed(read_history(reports_dir)):
        post = e.get("post") or {}
        if e.get("verdict") == "ADOPTED" and _match(post):
            sv = sv if sv is not None else post.get("setup_viol")
            hv = hv if hv is not None else post.get("hold_viol")
            break
    if (sv is None or hv is None) and logs_dir:
        lg = parse_eco_log(os.path.join(logs_dir, f"janus_eco_{mode}.log"))
        if _match(lg["post"]):
            sv = sv if sv is not None else lg["post"].get("setup_viol")
            hv = hv if hv is not None else lg["post"].get("hold_viol")
    rpt = os.path.join(reports_dir or "#", f"janus_eco_{mode}.rpt")
    if (sv is None or hv is None) and rpt_from_adopted(reports_dir, rpt, a):
        rs, rh = count_rpt_violators(rpt)
        sv = sv if sv is not None else rs
        hv = hv if hv is not None else rh
    sw, st = a.get("setup_ws"), a.get("setup_tns")
    if sv is None and isinstance(sw, (int, float)):
        if sw >= 0:
            sv = 0
        elif _same(sw, st):
            sv = 1
    hw = a.get("hold_ws")
    if hv is None and isinstance(hw, (int, float)) and hw >= 0:
        hv = 0
    return sv, hv


def eco_attempts(reports_dir: Optional[str], logs_dir: Optional[str],
                 agent_log: Optional[str] = None, run_start: Optional[float] = None,
                 adopted: Optional[Dict[str, object]] = None) -> List[Dict[str, object]]:
    """Every ECO pass of the current build, oldest first: history entries plus
    the latest janus_eco_<mode>.log when no history entry covers it."""
    fin = _mtime(os.path.join(reports_dir or "#", "6_finish.rpt")) or 0.0
    hist = [dict(e, _src="history") for e in read_history(reports_dir)
            if float(e.get("t") or 0) >= fin]
    a = adopted if adopted is not None else read_adopted(reports_dir)
    verdicts = None
    for lp in sorted(glob.glob(os.path.join(logs_dir or "#", "janus_eco_*.log"))):
        mode = os.path.basename(lp)[len("janus_eco_"):-len(".log")]
        lm = _mtime(lp) or 0.0
        if lm < fin or any(e.get("mode") == mode and float(e.get("t") or 0) >= lm - 60 for e in hist):
            continue
        lg = parse_eco_log(lp)
        if not (lg["pre"] or lg["post"] or lg["error"]):
            continue
        if verdicts is None:
            verdicts = _agent_verdicts(agent_log)
        mine = [v for v in verdicts if v["mode"] == mode]
        iv = [i for i, v in enumerate(mine) if v["what"] != "via ORFS make run"]
        last = mine[iv[-1]] if iv else None
        upto = iv[-1] if iv else len(mine)
        start = next((v for v in reversed(mine[:upto]) if v["what"] == "via ORFS make run"), None)
        post = dict(lg["post"])
        if lg["drc"] is not None:
            post["drc"] = lg["drc"]
        e: Dict[str, object] = {"mode": mode, "t": lm, "pre": dict(lg["pre"]), "post": post,
                                "margin": lg["margin"], "hold_bufs": lg["hold_bufs"],
                                "error": short_error(lg["error"], int(lg["open_nets"] or 0)),
                                "_src": "log", "wall_s": lg["wall_s"]}
        if a and a.get("mode") == mode and _same(post.get("setup_ws"), a.get("setup_ws")) \
                and _same(post.get("hold_ws"), a.get("hold_ws")) and lg["wrote"]:
            e["verdict"] = "ADOPTED"
            e["why"] = last["why"].split(";")[0] if last and last["what"] == "ADOPTED" else ""
        elif lg["error"]:
            e["verdict"], e["why"] = "DISCARDED", e["error"]
        elif last:
            e["verdict"], e["why"] = last["what"], last["why"].split("; keeping")[0]
        else:
            e["verdict"], e["why"] = "NOT ADOPTED", "no agent verdict found"
        if e["margin"] is None and start and start.get("margin") is not None:
            e["margin"] = start["margin"]
        if e["wall_s"] is None and start and last:
            e["wall_s"] = _dur_hhmmss(start["hh"], last["hh"])
        if e["wall_s"] is None and run_start and lm > run_start:
            e["wall_s"], e["wall_est"] = lm - run_start, True
        if not e["pre"] and a and (a.get("_mtime") or 0) < lm and a.get("mode") == mode:
            # truncated log: a pass on the ECO'd 6_final starts from the timing in force
            e["pre"] = {k: a.get(k) for k in ("setup_ws", "setup_tns", "hold_ws", "hold_tns")}
            e["pre_src"] = "in force"
        hist.append(e)
    hist.sort(key=lambda x: float(x.get("t") or 0))
    return hist


def _stamp(t) -> str:
    try:
        return datetime.fromtimestamp(float(t)).strftime("%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "?"


def eco_rows(reports_dir: Optional[str], logs_dir: Optional[str],
             agent_log: Optional[str] = None,
             run_start: Optional[float] = None,
             margin_hint=None) -> List[Dict[str, object]]:
    """Stage-table rows to insert after 6_finish: one per ECO attempt
    ('6_eco_hold 11-16 06:26', status ADOPTED/DISCARDED) then the ECO in force
    ('6_eco_hold (in force)', status IN FORCE). *margin_hint* (the block's
    JANUS_ECO_HOLD_MARGIN_PS now) fills the margin of the newest log-parsed
    hold attempt whose log lost its margin line, shown as 'margin N (cfg)'."""
    try:
        a = read_adopted(reports_dir)
        rows: List[Dict[str, object]] = []
        atts = eco_attempts(reports_dir, logs_dir, agent_log, run_start, a)
        for i, e in enumerate(atts):
            pre, post = e.get("pre") or {}, e.get("post") or {}
            if e.get("margin") is None and margin_hint not in (None, "") and e.get("_src") == "log" \
                    and e.get("mode") == "hold" and i == len(atts) - 1:
                try:
                    e["margin"], e["margin_cfg"] = float(margin_hint), True
                except (TypeError, ValueError):
                    pass
            rows.append({"key": f"6_eco_{e.get('mode') or '?'} {_stamp(e.get('t'))}", "_eco": "attempt",
                         "mode": e.get("mode"),
                         "status": str(e.get("verdict") or "?").upper(),
                         "why": e.get("why") or e.get("error") or "",
                         "wall": e.get("wall_s"), "wall_est": bool(e.get("wall_est")),
                         "cpu": None, "peak_kb": None, "margin": e.get("margin"),
                         "margin_cfg": bool(e.get("margin_cfg")), "pre": pre, "pre_src": e.get("pre_src"),
                         "setup_ws": post.get("setup_ws"), "setup_tns": post.get("setup_tns"),
                         "setup_viol": post.get("setup_viol"), "hold_ws": post.get("hold_ws"),
                         "hold_tns": post.get("hold_tns"), "hold_viol": post.get("hold_viol"),
                         "eco_drc": post.get("drc"), "hold_bufs_eco": e.get("hold_bufs"),
                         "src": e.get("_src")})
        if a:
            sv, hv = adopted_violators(reports_dir, logs_dir, a)
            rows.append({"key": f"6_eco_{a.get('mode') or 'hold'} (in force)", "_eco": "in_force",
                         "mode": a.get("mode") or "hold",
                         "status": "IN FORCE", "wall": None, "cpu": None, "peak_kb": None,
                         "setup_ws": a.get("setup_ws"), "setup_tns": a.get("setup_tns"),
                         "setup_viol": sv, "hold_ws": a.get("hold_ws"),
                         "hold_tns": a.get("hold_tns"), "hold_viol": hv,
                         "eco_drc": a.get("drc"), "at": a.get("at"), "margin": a.get("margin")})
        return rows
    except Exception:   # never break the email
        return []
