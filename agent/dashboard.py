#!/usr/bin/env python3
# ============================================================================
# dashboard.py — zero-dependency live + standalone run dashboard for the JANUS
# autonomous ASIC agent.
# ----------------------------------------------------------------------------
# WHAT / WHY
#   Long PD runs used to be observed only through a scrolling heartbeat line.
#   This module gives a proper EDA-style *browsable* dashboard (think the
#   run-status pages classic implementation tools shipped ~2010): one page that
#   shows every block, its live stage + progress + QoR, and — on click — the
#   REAL artifacts (per-block log, ORFS reports dir, results dir, metrics) with
#   their absolute filesystem paths, plus a to-scale SVG floorplan snapshot of
#   the implemented design.
#
# DESIGN PRINCIPLES (match the flow's house rules)
#   * ZERO third-party deps — stdlib http.server only. kiran's box is WSL2 and
#     often offline; no CDN, no pip install, no websockets. The SPA is a single
#     self-contained HTML string (inline CSS+JS) served from memory.
#   * PURE READER / DECOUPLED — the dashboard never mutates a run. In LIVE mode
#     the agent hands it a status dict (also mirrored to run_dir/dashboard_
#     state.json). In STANDALONE mode `python3 dashboard.py --run-dir <dir>`
#     reads that same JSON + scans ORFS output on disk, so ANY finished run can
#     be reopened and browsed later.
#   * DEFENSIVE — a dashboard bug must never crash or slow the real flow. The
#     server runs in a daemon thread; every handler is wrapped; file serving is
#     sandboxed to an explicit allow-list of roots (never serves outside them).
#   * DETERMINISTIC — the SVG snapshot is a pure function of the DEF + metrics.
# ============================================================================
from __future__ import annotations

import argparse
import glob
import html
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# Canonical major-stage order (mirrors janus_agent._STAGE_PATTERNS labels).
STAGES = ["1_synth", "2_floorplan", "3_place", "4_cts", "5_route", "6_final"]


def canon_stage_index(stage_label: str) -> int:
    """Map a verbose stage label (e.g. '3_place (global)') to 0..len(STAGES)-1.

    Returns -1 when nothing matches (e.g. 'starting'). Used to draw the N/M
    progress bar without the agent having to pass an index.
    """
    if not stage_label:
        return -1
    s = str(stage_label).strip().lower()
    for i, st in enumerate(STAGES):
        # match on the leading '<n>_<name>' token
        if s.startswith(st) or st.split("_", 1)[1] in s:
            return i
    return -1


def _read_json(path: str) -> dict:
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return {}


# --------------------------------------------------------------------------- #
# ORFS metrics + DEF parsing (for the QoR panel and the floorplan snapshot)
# --------------------------------------------------------------------------- #
def find_metrics_json(results_dir: str) -> str:
    """ORFS drops per-stage metadata-*.json next to the .odb results. Return the
    LATEST stage's metrics file (highest stage prefix), or '' if none."""
    if not results_dir or not os.path.isdir(results_dir):
        return ""
    cands = glob.glob(os.path.join(results_dir, "*metrics*.json"))
    cands += glob.glob(os.path.join(results_dir, "metadata-*.json"))
    # also look one level up (ORFS sometimes writes metrics under logs/)
    if not cands:
        return ""
    return sorted(cands)[-1]


def summarize_metrics(m: dict) -> dict:
    """Pull the handful of universally-interesting QoR/geometry numbers out of
    an ORFS metrics dict, tolerant to the stage prefix (synth__/floorplan__/
    detailedroute__ ...). Returns a flat, display-ready dict."""
    out: dict = {}
    if not m:
        return out

    def pick(*needles):
        for k, v in m.items():
            kl = k.lower()
            if all(n in kl for n in needles):
                return v
        return None

    out["die_area_um2"] = pick("design", "die", "area")
    out["core_area_um2"] = pick("design", "core", "area")
    out["utilization"] = pick("design", "instance", "utilization") \
        if pick("design", "instance", "utilization") is not None else pick("utilization")
    out["insts"] = pick("design", "instance", "count")
    out["macros"] = pick("instance", "count", "macros")
    out["stdcells"] = pick("instance", "count", "stdcell")
    out["nets"] = pick("design", "nets")
    out["io"] = pick("design", "io")
    # timing (ps in asap7); keys vary a lot across stages, best-effort
    out["wns"] = pick("timing", "setup", "ws") or pick("timing", "setup", "wns")
    out["tns"] = pick("timing", "setup", "tns")
    out["drc"] = pick("drc", "errors") or pick("route", "drc")
    return {k: v for k, v in out.items() if v is not None}


_DIEAREA_RE = re.compile(r"DIEAREA\s+\(\s*(-?\d+)\s+(-?\d+)\s*\)\s*\(\s*(-?\d+)\s+(-?\d+)\s*\)")
_UNITS_RE = re.compile(r"UNITS\s+DISTANCE\s+MICRONS\s+(\d+)")
# DEF component line:  - <inst> <MACRO> + (FIXED|PLACED|SOURCE ...) ( x y ) <ORIENT> ;
_COMP_RE = re.compile(
    r"^\s*-\s+(\S+)\s+(\S+)\s+.*?\+\s+(?:FIXED|PLACED|COVER)\s*\(\s*(-?\d+)\s+(-?\d+)\s*\)\s*(\w+)")


def _lef_macro_sizes(lef_dirs) -> dict:
    """Best-effort MACRO -> (w_um, h_um) from any LEFs in the given dirs, so the
    floorplan snapshot can draw macros to scale. Missing sizes are fine."""
    sizes: dict = {}
    for d in lef_dirs or []:
        if not d or not os.path.isdir(d):
            continue
        for lef in glob.glob(os.path.join(d, "*.lef")):
            name = None
            try:
                for ln in open(lef, errors="replace"):
                    ms = re.match(r"\s*MACRO\s+(\S+)", ln)
                    if ms:
                        name = ms.group(1)
                        continue
                    msz = re.match(r"\s*SIZE\s+([\d.]+)\s+BY\s+([\d.]+)", ln)
                    if msz and name:
                        sizes[name] = (float(msz.group(1)), float(msz.group(2)))
                        name = None
            except Exception:
                continue
    return sizes


def parse_def_floorplan(def_path: str, lef_dirs=None, macro_limit: int = 4000) -> dict:
    """Parse a DEF into a compact floorplan model for SVG rendering:
    {units, die:[x0,y0,x1,y1] (um), macros:[{inst,cell,x,y,w,h,orient}], n_comp}.
    Only hard MACRO components are kept (std cells are skipped by size lookup —
    if a cell has no LEF SIZE it is treated as a std cell and drawn as a dot).
    Fully defensive; returns {} on any trouble."""
    if not def_path or not os.path.isfile(def_path):
        return {}
    units = 2000.0
    die = None
    macros = []
    dots = 0
    sizes = _lef_macro_sizes(lef_dirs)
    try:
        with open(def_path, errors="replace") as fh:
            in_comps = False
            for ln in fh:
                if die is None:
                    md = _DIEAREA_RE.search(ln)
                    if md:
                        die = [int(md.group(1)), int(md.group(2)),
                               int(md.group(3)), int(md.group(4))]
                        continue
                mu = _UNITS_RE.search(ln)
                if mu:
                    units = float(mu.group(1))
                    continue
                if ln.startswith("COMPONENTS"):
                    in_comps = True
                    continue
                if ln.startswith("END COMPONENTS"):
                    in_comps = False
                    continue
                if in_comps and len(macros) < macro_limit:
                    mc = _COMP_RE.match(ln)
                    if mc:
                        inst, cell, x, y, orient = mc.groups()
                        wh = sizes.get(cell)
                        if wh:  # a real hard macro (has a LEF SIZE)
                            macros.append({
                                "inst": inst, "cell": cell,
                                "x": int(x) / units, "y": int(y) / units,
                                "w": wh[0], "h": wh[1], "orient": orient,
                            })
                        else:
                            dots += 1
    except Exception:
        return {}
    if die is None:
        return {}
    die_um = [d / units for d in die]
    return {"units": units, "die_um": die_um, "macros": macros,
            "stdcell_dots": dots}


def render_floorplan_svg(fp: dict, metrics: dict, title: str = "") -> str:
    """Deterministic inline SVG of the implemented floorplan: die outline, macro
    rectangles to scale, a utilization bar and a compact stats legend. Falls
    back to a metrics-only schematic when no DEF geometry is available."""
    W, H = 720, 560
    pad = 30
    plot_w, plot_h = W - 2 * pad, H - 2 * pad - 70  # leave room for legend
    parts = [
        f'<svg xmlns="https://images.samsung.com/is/image/samsung/africa_en-feature-large-audience-sharing-192934244?$FB_TYPE_A_MO_JPG$" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" font-family="ui-monospace,Menlo,monospace">',
        f'<rect x="0" y="0" width="{W}" height="{H}" fill="#0d1117"/>',
    ]
    if title:
        parts.append(f'<text x="{pad}" y="20" fill="#58a6ff" font-size="14">'
                     f'{html.escape(title)}</text>')

    die = fp.get("die_um") if fp else None
    if die:
        dx0, dy0, dx1, dy1 = die
        dw, dh = max(dx1 - dx0, 1e-6), max(dy1 - dy0, 1e-6)
        scale = min(plot_w / dw, plot_h / dh)
        ox, oy = pad, pad + 30

        def sx(x):
            return ox + (x - dx0) * scale

        def sy(y):  # DEF y is bottom-up; SVG y is top-down → flip
            return oy + (dy1 - y) * scale

        parts.append(
            f'<rect x="{sx(dx0):.1f}" y="{sy(dy1):.1f}" width="{dw*scale:.1f}" '
            f'height="{dh*scale:.1f}" fill="#161b22" stroke="#30363d" '
            f'stroke-width="1.5"/>')
        for mac in fp.get("macros", []):
            mx, my = sx(mac["x"]), sy(mac["y"] + mac["h"])
            mw, mh = mac["w"] * scale, mac["h"] * scale
            parts.append(
                f'<rect x="{mx:.1f}" y="{my:.1f}" width="{max(mw,1):.1f}" '
                f'height="{max(mh,1):.1f}" fill="#1f6feb" fill-opacity="0.55" '
                f'stroke="#58a6ff" stroke-width="0.4">'
                f'<title>{html.escape(mac["inst"])}\n{html.escape(mac["cell"])}'
                f'  {mac["w"]:.2f}x{mac["h"]:.2f}um</title></rect>')
        parts.append(
            f'<text x="{ox}" y="{oy - 8}" fill="#8b949e" font-size="11">'
            f'die {dw:.1f} x {dh:.1f} um   macros drawn: '
            f'{len(fp.get("macros", []))}   std-cells: '
            f'{fp.get("stdcell_dots", 0)}</text>')
    else:
        parts.append(
            f'<text x="{W/2:.0f}" y="{H/2 - 10:.0f}" fill="#8b949e" '
            f'font-size="13" text-anchor="middle">no DEF geometry yet — '
            f'showing metrics summary below</text>')

    # legend / QoR strip along the bottom (always shown)
    ly = H - 56
    util = metrics.get("utilization")
    parts.append(f'<text x="{pad}" y="{ly}" fill="#c9d1d9" font-size="12">'
                 f'utilization</text>')
    bar_w = 240
    parts.append(f'<rect x="{pad+90}" y="{ly-11}" width="{bar_w}" height="12" '
                 f'fill="#161b22" stroke="#30363d"/>')
    if isinstance(util, (int, float)):
        frac = max(0.0, min(1.0, util if util <= 1 else util / 100.0))
        col = "#2ea043" if frac < 0.75 else ("#d29922" if frac < 0.9 else "#f85149")
        parts.append(f'<rect x="{pad+90}" y="{ly-11}" width="{bar_w*frac:.1f}" '
                     f'height="12" fill="{col}"/>')
        parts.append(f'<text x="{pad+90+bar_w+8}" y="{ly}" fill="#c9d1d9" '
                     f'font-size="12">{frac*100:.1f}%</text>')

    def stat(x, y, label, val):
        if val is None:
            return
        if isinstance(val, float):
            val = f"{val:,.1f}"
        parts.append(f'<text x="{x}" y="{y}" fill="#8b949e" font-size="11">'
                     f'{label}: <tspan fill="#c9d1d9">{val}</tspan></text>')

    row = H - 30
    stat(pad, row, "die um2", metrics.get("die_area_um2"))
    stat(pad + 170, row, "core um2", metrics.get("core_area_um2"))
    stat(pad + 350, row, "insts", metrics.get("insts"))
    stat(pad + 500, row, "macros", metrics.get("macros"))
    row2 = H - 14
    stat(pad, row2, "nets", metrics.get("nets"))
    stat(pad + 170, row2, "IO", metrics.get("io"))
    stat(pad + 350, row2, "WNS", metrics.get("wns"))
    stat(pad + 500, row2, "DRC", metrics.get("drc"))
    parts.append("</svg>")
    return "".join(parts)


# --------------------------------------------------------------------------- #
# Live status holder (agent side)
# --------------------------------------------------------------------------- #
class DashboardState:
    """Thread-safe container the agent updates; also mirrored to disk so a
    standalone dashboard (or a browser reconnect) sees the same picture."""

    def __init__(self, run_dir: str):
        self.run_dir = run_dir
        self._lock = threading.Lock()
        self._data: dict = {"run_dir": run_dir, "blocks": {}, "meta": {}}
        self._path = os.path.join(run_dir, "dashboard_state.json")

    def set_meta(self, **kw):
        with self._lock:
            self._data["meta"].update(kw)
        self._flush()

    def update_block(self, block: str, **kw):
        with self._lock:
            b = self._data["blocks"].setdefault(block, {"block": block})
            b.update(kw)
            b["updated"] = time.time()
        self._flush()

    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._data))

    def _flush(self):
        try:
            tmp = self._path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(self._data, fh)
            os.replace(tmp, self._path)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# HTTP server
# --------------------------------------------------------------------------- #
class _Handler(BaseHTTPRequestHandler):
    server_version = "JANUSDash/1.0"

    # injected by Dashboard: .dash
    def log_message(self, *a):  # silence stdlib access logging
        return

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode("utf-8", "replace")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass

    def _safe(self, path: str):
        """Resolve a requested path and confirm it is inside an allow-listed
        root. Returns the real path or None (never serves outside roots)."""
        if not path:
            return None
        rp = os.path.realpath(path)
        for root in self.dash.roots:
            if root and (rp == root or rp.startswith(root + os.sep)):
                return rp
        return None

    def do_GET(self):
        try:
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path in ("/", "/index.html"):
                return self._send(200, INDEX_HTML, "text/html; charset=utf-8")
            if u.path == "/api/status":
                return self._send(200, json.dumps(self.dash.build_status()))
            if u.path == "/api/log":
                return self._api_log(q)
            if u.path == "/api/file":
                return self._api_file(q)
            if u.path == "/api/snapshot":
                return self._api_snapshot(q)
            return self._send(404, json.dumps({"error": "not found"}))
        except Exception as e:  # never let a handler bug bubble up
            return self._send(500, json.dumps({"error": str(e)}))

    def _api_log(self, q):
        block = (q.get("block") or [""])[0]
        tail = int((q.get("tail") or ["400"])[0])
        st = self.dash.build_status()
        binfo = st["blocks"].get(block, {})
        lp = binfo.get("log_path", "")
        rp = self._safe(lp)
        if not rp or not os.path.isfile(rp):
            return self._send(200, json.dumps(
                {"block": block, "path": lp, "text": "(log not found yet)"}))
        try:
            with open(rp, errors="replace") as fh:
                lines = fh.readlines()
            text = "".join(lines[-tail:])
        except Exception as e:
            text = f"(could not read log: {e})"
        return self._send(200, json.dumps(
            {"block": block, "path": rp, "lines": len(lines), "text": text}))

    def _api_file(self, q):
        path = (q.get("path") or [""])[0]
        rp = self._safe(path)
        if not rp or not os.path.isfile(rp):
            return self._send(403, json.dumps({"error": "path not allowed"}))
        ext = os.path.splitext(rp)[1].lower()
        ctype = {".json": "application/json", ".rpt": "text/plain",
                 ".log": "text/plain", ".txt": "text/plain",
                 ".def": "text/plain", ".tcl": "text/plain"}.get(
                     ext, "text/plain; charset=utf-8")
        try:
            with open(rp, "rb") as fh:
                data = fh.read()
        except Exception as e:
            return self._send(500, json.dumps({"error": str(e)}))
        return self._send(200, data, ctype)

    def _api_snapshot(self, q):
        block = (q.get("block") or [""])[0]
        svg = self.dash.build_snapshot(block)
        return self._send(200, svg, "image/svg+xml")


class Dashboard:
    """Owns the HTTP server + the on-disk/live status source. Use in LIVE mode
    (agent passes a DashboardState) or STANDALONE mode (run_dir only)."""

    def __init__(self, run_dir: str, port: int = 8080, host: str = "0.0.0.0",
                 state: DashboardState = None, extra_roots=None):
        self.run_dir = os.path.realpath(run_dir)
        self.port = port
        self.host = host
        self.state = state
        # allow-list of dirs the file/log endpoints may read from
        roots = [self.run_dir] + list(extra_roots or [])
        self.roots = [os.path.realpath(r) for r in roots if r]
        self._httpd = None
        self._thread = None
        # cache of resolved ORFS dirs per block (filled by janus_agent or scan)
        self._dir_hints: dict = {}

    # ---- status assembly (merges live state + on-disk dashboard_state.json) --
    def build_status(self) -> dict:
        data = {}
        if self.state is not None:
            data = self.state.snapshot()
        else:
            data = _read_json(os.path.join(self.run_dir, "dashboard_state.json"))
        data.setdefault("run_dir", self.run_dir)
        data.setdefault("blocks", {})
        data.setdefault("meta", {})
        # enrich each block with a canonical stage index + resolved artifact dirs
        for name, b in data["blocks"].items():
            b["stage_index"] = canon_stage_index(b.get("stage", ""))
            b["n_stages"] = len(STAGES)
            b.setdefault("log_path",
                         os.path.join(self.run_dir, f"{name}.log"))
        data["now"] = time.time()
        return data

    def build_snapshot(self, block: str) -> str:
        st = self.build_status()
        b = st["blocks"].get(block, {})
        results_dir = b.get("results_dir") or ""
        lef_dirs = []
        if b.get("sram_dir"):
            lef_dirs.append(b["sram_dir"])
        def_path = ""
        if results_dir and os.path.isdir(results_dir):
            defs = sorted(glob.glob(os.path.join(results_dir, "*.def")))
            if defs:
                def_path = defs[-1]
            lef_dirs.append(results_dir)
        fp = parse_def_floorplan(def_path, lef_dirs) if def_path else {}
        metrics = summarize_metrics(_read_json(find_metrics_json(results_dir)))
        title = f"{block}  —  floorplan snapshot"
        return render_floorplan_svg(fp, metrics, title)

    # ---- lifecycle -----------------------------------------------------------
    def start(self) -> str:
        handler = type("H", (_Handler,), {"dash": self})
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="janus-dashboard",
            daemon=True)
        self._thread.start()
        return f"http://localhost:{self.port}/"

    def stop(self):
        try:
            if self._httpd:
                self._httpd.shutdown()
                self._httpd.server_close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# The single-page app (inline; no external assets)
# --------------------------------------------------------------------------- #
INDEX_HTML = r"""<!doctype html><html><head><meta charset="utf-8">
<title>JANUS — run dashboard</title>
<style>
:root{--bg:#0d1117;--panel:#161b22;--line:#30363d;--fg:#c9d1d9;--muted:#8b949e;--acc:#58a6ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:13px/1.5 ui-monospace,Menlo,Consolas,monospace}
header{padding:12px 18px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:16px;flex-wrap:wrap}
h1{font-size:16px;margin:0;color:var(--acc);letter-spacing:1px}
.pill{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:2px 10px;color:var(--muted)}
.wrap{display:grid;grid-template-columns:1fr 1fr;gap:14px;padding:16px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px}
.card h3{margin:0 0 8px;font-size:14px;display:flex;justify-content:space-between;align-items:center}
.st{font-size:11px;padding:1px 8px;border-radius:10px;text-transform:uppercase;letter-spacing:.5px}
.DONE{background:#12341f;color:#3fb950;border:1px solid #238636}
.RUNNING{background:#0f2942;color:#58a6ff;border:1px solid #1f6feb}
.FAILED{background:#3a1418;color:#f85149;border:1px solid #da3633}
.PENDING,.BLOCKED{background:#21262d;color:#8b949e;border:1px solid #30363d}
.bar{height:8px;background:#0d1117;border:1px solid var(--line);border-radius:5px;overflow:hidden;margin:6px 0}
.bar>i{display:block;height:100%;background:linear-gradient(90deg,#1f6feb,#58a6ff)}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:4px 10px;margin:8px 0;font-size:12px}
.grid b{color:#fff;font-weight:600}
.k{color:var(--muted)}
.links a{color:var(--acc);text-decoration:none;margin-right:12px;cursor:pointer}
.links a:hover{text-decoration:underline}
.path{color:var(--muted);font-size:11px;word-break:break-all;margin-top:4px}
#detail{grid-column:1/3}
pre{background:#010409;border:1px solid var(--line);border-radius:6px;padding:10px;max-height:420px;overflow:auto;white-space:pre-wrap;word-break:break-word}
.tabs a{cursor:pointer;color:var(--muted);margin-right:14px}
.tabs a.on{color:var(--acc);border-bottom:2px solid var(--acc);padding-bottom:3px}
.stage{color:var(--muted);font-size:12px}
svg{max-width:100%;height:auto;border:1px solid var(--line);border-radius:6px}
</style></head><body>
<header>
  <h1>◈ JANUS</h1>
  <span class="pill" id="run">run —</span>
  <span class="pill" id="head">git —</span>
  <span class="pill" id="clock"></span>
  <span class="pill" id="prog">0/0 done</span>
</header>
<div class="wrap" id="cards"></div>
<script>
let SEL=null, TAB='log';
function esc(s){return (s==null?'':String(s)).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function num(v){return (v==null)?'—':(typeof v==='number'? v.toLocaleString():v)}
async function jget(u){const r=await fetch(u);return r.json()}
function stageName(i,n,label){if(i<0)return label||'—';return (i+1)+'/'+n+'  '+(label||'')}
async function tick(){
 let s;try{s=await jget('/api/status')}catch(e){return}
 document.getElementById('run').textContent='run '+(s.run_dir||'—');
 document.getElementById('head').textContent='git '+((s.meta&&s.meta.git_head)||'—');
 document.getElementById('clock').textContent=new Date().toLocaleTimeString();
 const bs=s.blocks||{};const names=Object.keys(bs).sort();
 let done=names.filter(n=>bs[n].status==='DONE').length;
 document.getElementById('prog').textContent=done+'/'+names.length+' done';
 const host=document.getElementById('cards');
 let html='';
 for(const n of names){const b=bs[n];
  const idx=b.stage_index==null?-1:b.stage_index, ns=b.n_stages||6;
  const frac=idx<0?0:Math.round((idx+1)/ns*100);
  html+=`<div class="card"><h3>${esc(n)} <span class="st ${b.status||'PENDING'}">${esc(b.status||'PENDING')}</span></h3>`;
  html+=`<div class="stage">${esc(stageName(idx,ns,b.stage))} <span style="float:right">elapsed ${esc(b.elapsed||'—')} · ${num(b.lines)} lines</span></div>`;
  html+=`<div class="bar"><i style="width:${frac}%"></i></div>`;
  html+=`<div class="grid">
   <span class="k">WNS(ps)</span><b>${num(b.wns_ns)}</b><span class="k">hold(ps)</span><b>${num(b.hold_ps)}</b>
   <span class="k">TNS(ps)</span><b>${num(b.tns_ns)}</b><span class="k">DRC</span><b>${num(b.drc_violations)}</b>
   <span class="k">util</span><b>${num(b.utilization)}</b><span class="k">area</span><b>${num(b.area)}</b>
   <span class="k">tries</span><b>${num(b.tries)}</b><span class="k"></span><b></b></div>`;
  if(b.note) html+=`<div class="path">note: ${esc(b.note)}</div>`;
  html+=`<div class="links">
    <a onclick="open_('${n}','log')">▤ log</a>
    <a onclick="open_('${n}','snap')">▦ floorplan</a>`;
  if(b.reports_dir) html+=`<a onclick="openFile('${esc(b.reports_dir)}')">▧ reports</a>`;
  if(b.results_dir) html+=`<a onclick="openFile('${esc(b.results_dir)}')">▤ results</a>`;
  html+=`</div><div class="path">log: ${esc(b.log_path||'')}</div>`;
  if(b.results_dir)html+=`<div class="path">results: ${esc(b.results_dir)}</div>`;
  html+=`</div>`;
 }
 html+=`<div class="card" id="detail"><h3><span id="dtitle">select a block</span>
   <span class="tabs"><a id="tlog" onclick="setTab('log')">log</a><a id="tsnap" onclick="setTab('snap')">floorplan</a></span></h3>
   <div id="dbody"><span class="k">Click ▤ log or ▦ floorplan on a block above.</span></div></div>`;
 host.innerHTML=html;
 if(SEL) render();
}
function setTab(t){TAB=t;render()}
function open_(n,t){SEL=n;TAB=(t==='snap'?'snap':'log');render()}
function openFile(p){SEL_PATH=p;window.open('/api/file?path='+encodeURIComponent(p),'_blank')}
let SEL_PATH=null;
async function render(){
 if(!SEL)return;
 document.getElementById('dtitle').textContent=SEL+'  ·  '+TAB;
 document.getElementById('tlog').className=TAB==='log'?'on':'';
 document.getElementById('tsnap').className=TAB==='snap'?'on':'';
 const body=document.getElementById('dbody');
 if(TAB==='snap'){
   body.innerHTML=`<img alt="floorplan" src="/api/snapshot?block=${encodeURIComponent(SEL)}&_=${Date.now()}">`;
   return;
 }
 try{const d=await jget('/api/log?block='+encodeURIComponent(SEL)+'&tail=500');
   body.innerHTML=`<div class="path">${esc(d.path||'')} — ${num(d.lines)} lines</div><pre>${esc(d.text||'')}</pre>`;
   const pre=body.querySelector('pre'); if(pre)pre.scrollTop=pre.scrollHeight;
 }catch(e){body.innerHTML='<span class="k">log unavailable</span>'}
}
tick(); setInterval(tick, 2500);
</script></body></html>"""


# --------------------------------------------------------------------------- #
# Standalone entry point:  python3 dashboard.py --run-dir <dir> [--port 8080]
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(
        description="Browse a JANUS agent run (live state file + ORFS output) "
                    "in your browser. Point it at any runs/<ts> directory.")
    ap.add_argument("--run-dir", required=True,
                    help="a runs/<timestamp> dir (contains dashboard_state.json "
                         "and/or the per-block .log files)")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--flow-dir", default=None,
                    help="ORFS flow/ dir, so reports/results become browsable")
    args = ap.parse_args()
    extra = []
    if args.flow_dir:
        extra += [os.path.join(args.flow_dir, "results"),
                  os.path.join(args.flow_dir, "reports"),
                  os.path.join(args.flow_dir, "logs")]
    dash = Dashboard(args.run_dir, port=args.port, extra_roots=extra)
    url = dash.start()
    print(f"[janus-dashboard] serving {args.run_dir}")
    print(f"[janus-dashboard] open {url}  (Ctrl-C to stop)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        dash.stop()
        print("\n[janus-dashboard] stopped")


if __name__ == "__main__":
    main()
