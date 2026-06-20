"""
reporter.py — Generate the final run report (HTML + JSON + CSV).

Consumes the per-block results the orchestrator collected (status, attempts,
and any metrics the LogAnalyzer extracted) and produces:
  * report.json  — machine-readable
  * report.csv   — spreadsheet-friendly PPA table
  * report.html  — human-friendly summary

HONESTY: metrics are only shown when they were actually parsed from an OpenROAD
report. Missing values are rendered as "n/a" — we never fill blanks with guesses.
"""

from __future__ import annotations

import csv
import html
import json
import os
import time
from typing import Dict, List, Any


_COLUMNS = [
    ("block", "Block"),
    ("status", "Status"),
    ("attempts", "Attempts"),
    ("last_stage", "Last stage"),
    ("wns_ns", "WNS (ns)"),
    ("tns_ns", "TNS (ns)"),
    ("drc_violations", "DRC"),
    ("area", "Area"),
    ("power", "Power"),
    ("utilization", "Util %"),
    ("llm_calls", "LLM calls"),
]


def _fmt(v: Any) -> str:
    if v is None or v == "":
        return "n/a"
    if isinstance(v, float):
        return f"{v:.4g}"
    return str(v)


class Reporter:
    def __init__(self, out_dir: str):
        self.out_dir = out_dir
        os.makedirs(out_dir, exist_ok=True)

    def write_all(self, blocks: List[Dict], usage: Dict, meta: Dict) -> Dict[str, str]:
        paths = {
            "json": os.path.join(self.out_dir, "report.json"),
            "csv": os.path.join(self.out_dir, "report.csv"),
            "html": os.path.join(self.out_dir, "report.html"),
        }
        self._write_json(paths["json"], blocks, usage, meta)
        self._write_csv(paths["csv"], blocks)
        self._write_html(paths["html"], blocks, usage, meta)
        return paths

    # ---- JSON ---------------------------------------------------------------
    def _write_json(self, path, blocks, usage, meta):
        with open(path, "w") as fh:
            json.dump({"meta": meta, "usage": usage, "blocks": blocks}, fh, indent=2, sort_keys=True)

    # ---- CSV ----------------------------------------------------------------
    def _write_csv(self, path, blocks):
        with open(path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow([h for _, h in _COLUMNS])
            for b in blocks:
                w.writerow([_fmt(b.get(k)) for k, _ in _COLUMNS])

    # ---- HTML ---------------------------------------------------------------
    def _write_html(self, path, blocks, usage, meta):
        def badge(status):
            color = {"DONE": "#1a7f37", "FAILED": "#cf222e", "BLOCKED": "#9a6700",
                     "RUNNING": "#0969da", "PENDING": "#57606a"}.get(status, "#57606a")
            return f'<span style="background:{color};color:#fff;padding:2px 8px;border-radius:10px;font-size:12px">{html.escape(status)}</span>'

        rows = []
        for b in blocks:
            cells = []
            for k, _ in _COLUMNS:
                if k == "status":
                    cells.append(f"<td>{badge(b.get('status','PENDING'))}</td>")
                else:
                    cells.append(f"<td>{html.escape(_fmt(b.get(k)))}</td>")
            rows.append("<tr>" + "".join(cells) + "</tr>")

        done = sum(1 for b in blocks if b.get("status") == "DONE")
        total = len(blocks)
        head = "".join(f"<th style='text-align:left'>{html.escape(h)}</th>" for _, h in _COLUMNS)

        doc = f"""<!doctype html><html><head><meta charset="utf-8">
<title>JANUS ASIC flow report</title>
<style>
 body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:24px;color:#1f2328}}
 table{{border-collapse:collapse;width:100%;margin-top:12px}}
 th,td{{border:1px solid #d0d7de;padding:6px 10px;font-size:14px}}
 th{{background:#f6f8fa}}
 .note{{background:#fff8c5;border:1px solid #d4a72c;padding:10px 14px;border-radius:6px;margin:14px 0}}
 code{{background:#eff1f3;padding:1px 5px;border-radius:4px}}
</style></head><body>
<h1>JANUS ASIC flow — run report</h1>
<p>Generated {html.escape(time.strftime('%Y-%m-%d %H:%M:%S'))} ·
   Blocks done: <b>{done}/{total}</b> ·
   PDK: {html.escape(str(meta.get('platform','asap7')))} ·
   ORFS: <code>{html.escape(str(meta.get('orfs_dir','?')))}</code></p>
<div class="note"><b>Honesty note.</b> WNS/TNS/area/power/util below are parsed
 directly from OpenROAD stage reports for this run. Blanks show <code>n/a</code>
 (not measured). These are real flow outputs for the given RTL + ASAP7 PDK +
 target constraints; they are NOT vendor datasheet numbers and depend on the
 SRAM macros / library you supplied.</div>
<table><thead><tr>{head}</tr></thead><tbody>
{''.join(rows)}
</tbody></table>
<h2>LLM / credit usage (estimate)</h2>
<ul>
 <li>LLM calls: {html.escape(str(usage.get('llm_calls','0')))}</li>
 <li>Approx tokens: {html.escape(str(usage.get('approx_tokens','0')))}
     <small>(~4 chars/token estimate — confirm actual credits in your Abacus dashboard)</small></li>
</ul>
</body></html>"""
        with open(path, "w") as fh:
            fh.write(doc)


# ---- self-test --------------------------------------------------------------
if __name__ == "__main__":
    import tempfile
    d = tempfile.mkdtemp()
    blocks = [
        {"block": "security", "status": "DONE", "attempts": 1, "last_stage": "route",
         "wns_ns": 0.05, "tns_ns": 0.0, "drc_violations": 0, "area": 1234.5,
         "power": 0.0012, "utilization": 55, "llm_calls": 0},
        {"block": "ampo", "status": "FAILED", "attempts": 3, "last_stage": "synth", "llm_calls": 2},
    ]
    r = Reporter(d)
    paths = r.write_all(blocks, {"llm_calls": 2, "approx_tokens": 3100}, {"platform": "asap7", "orfs_dir": "/x"})
    assert all(os.path.isfile(p) for p in paths.values())
    print("reporter self-test OK:", paths)
