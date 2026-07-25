#!/usr/bin/env python3
"""
mcp_server.py — a Model Context Protocol (MCP) surface over the JANUS ASIC
agent's DETERMINISTIC, read-only investigators.

Item 1 of the vibe-ic integration (see docs/VIBEIC_EDA_EVALUATION.md section 8).

WHY THIS EXISTS
---------------
The agent's real value is not the LLM — it is the SCAFFOLDING around it
(reasoning_engine, stage_timing_analyzer, orfs_probe). Today only janus_agent's
own fail loop can call that scaffolding. Exposing it over MCP lets ANY MCP client
— Claude Desktop, Continue, Cline, LM Studio, an open-source model driven through
an MCP-capable client, or a plain `mcp` CLI — ask the SAME precisely-framed
engineering questions a human lead would, backed by the same deterministic
evidence. The model is deliberately interchangeable: MCP is a wire protocol, so
whichever model the user prefers (open-source included) plugs in unchanged.

DESIGN
------
1. MODEL-AGNOSTIC / TRANSPORT-STANDARD. Speaks MCP over stdio (the most widely
   supported transport). No model, vendor, or key is baked in.
2. ZERO HARD DEPENDENCY. If the official `mcp` Python SDK is installed we use it
   (preferred, future-proof). If it is NOT, we fall back to a self-contained
   stdlib JSON-RPC 2.0 stdio loop implementing the MCP core methods
   (`initialize`, `tools/list`, `tools/call`). Either way the server runs with no
   pip install and no network — matching the rest of the agent.
3. READ-ONLY BY DEFAULT. Every default tool only READS logs/reports/odb or runs
   an already-read-only probe. Nothing mutates a flow. Guarded ACTION tools
   (e.g. triggering a build) are dry-run unless JANUS_MCP_ALLOW_ACTIONS=1 is set
   explicitly, and even then they only shell out to janus_agent — the single
   owner of any real mutation.
4. SINGLE SOURCE OF TRUTH. Both front-ends dispatch through the same TOOLS
   registry, so the SDK and fallback paths can never drift.

Run:
    python3 mcp_server.py                 # stdio MCP server (add to your client)
    python3 mcp_server.py --list-tools    # print the tool catalogue and exit
    python3 mcp_server.py --selftest      # exercise the read-only tools offline
"""

from __future__ import annotations

import glob as _glob
import json
import os
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple

# Make sibling agent modules importable no matter the CWD the client launches us
# from (MCP clients typically spawn the server with an arbitrary working dir).
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import orfs_probe            # noqa: E402  (read-only .odb probe)
import reasoning_engine      # noqa: E402  (deterministic failure classifier)

try:
    import stage_timing_analyzer as _sta   # noqa: E402
except Exception:                          # pragma: no cover
    _sta = None

try:
    from dependency_manager import DEFAULT_GRAPH  # noqa: E402
except Exception:                                 # pragma: no cover
    DEFAULT_GRAPH = {}

SERVER_NAME = "janus-asic"
SERVER_VERSION = "0.1.0"


# --------------------------------------------------------------------------- #
# Path helpers — standalone mirrors of janus_agent's locators, so the server
# does NOT need to instantiate the whole agent (which creates run dirs, etc.).
# --------------------------------------------------------------------------- #
def _reports_dir_for(flow_dir: str, block: str) -> Optional[str]:
    cands = [
        os.path.join(flow_dir, "reports", "asap7", block, "base"),
        os.path.join(flow_dir, "reports", "asap7", f"janus_{block}", "base"),
    ]
    cands += _glob.glob(os.path.join(flow_dir, "reports", "*", f"*{block}*", "base"))
    for d in cands:
        if os.path.isdir(d) and _glob.glob(os.path.join(d, "*_*.rpt")):
            return d
    return None


def _results_dir_for(flow_dir: str, block: str) -> Optional[str]:
    cands = [
        os.path.join(flow_dir, "results", "asap7", block, "base"),
        os.path.join(flow_dir, "results", "asap7", f"janus_{block}", "base"),
        os.path.join(flow_dir, "results", "asap7", "janus", block, "base"),
    ]
    cands += _glob.glob(os.path.join(flow_dir, "results", "*", f"*{block}*", "base"))
    for d in cands:
        if os.path.isdir(d) and _glob.glob(os.path.join(d, "*.odb")):
            return d
    return None


def _split_lines(text: str) -> List[str]:
    return [ln for ln in (text or "").splitlines() if ln.strip()]


# --------------------------------------------------------------------------- #
# Tool handlers — every one returns a JSON-serialisable dict.
# --------------------------------------------------------------------------- #
def t_list_blocks(_: Dict[str, Any]) -> Dict[str, Any]:
    """List the JANUS block dependency graph and a valid build order."""
    graph = {b: sorted(deps) for b, deps in DEFAULT_GRAPH.items()}
    # deterministic topological order (Kahn), ties broken alphabetically.
    order: List[str] = []
    remaining = {b: set(d) for b, d in DEFAULT_GRAPH.items()}
    while remaining:
        ready = sorted(b for b, d in remaining.items() if not (d - set(order)))
        if not ready:
            break  # (graph is validated acyclic elsewhere; guard anyway)
        order.extend(ready)
        for b in ready:
            remaining.pop(b, None)
    return {"blocks": graph, "build_order": order, "count": len(graph)}


def t_reason_about_failure(a: Dict[str, Any]) -> Dict[str, Any]:
    """Classify a failure from log text alone (no reports needed).

    args: error_text (str, required), block (str), stage_hint (str), tail (str)
    """
    error_text = a.get("error_text", "")
    if not error_text:
        return {"error": "error_text is required"}
    block = a.get("block", "unknown")
    tail = a.get("tail", "")
    stage_hint = a.get("stage_hint")
    dep = {k: set(v) for k, v in DEFAULT_GRAPH.items()} or {block: set()}
    if block not in dep:
        dep[block] = set()
    r = reasoning_engine.reason(_split_lines(error_text), tail, block, dep, stage_hint)
    return {
        "block": block,
        "failing_stage": r.failing_stage,
        "tool_bug_suspected": r.tool_bug_suspected,
        "can_auto": r.can_auto,
        "needs_llm": r.needs_llm,
        "top": (None if not r.top else {
            "signature": r.top.signature, "layer": r.top.layer,
            "confidence": r.top.confidence, "deterministic": r.top.deterministic,
            "root_cause": r.top.root_cause, "remedy_hint": r.top.remedy_hint,
            "downstream": r.top.downstream, "ripple_blocks": r.top.ripple_blocks,
            "evidence": r.top.evidence,
        }),
        "hypotheses": [{
            "signature": h.signature, "layer": h.layer, "confidence": h.confidence,
            "deterministic": h.deterministic, "root_cause": h.root_cause,
        } for h in r.hypotheses[:5]],
        "briefing": r.briefing,
    }


def t_analyze_reports(a: Dict[str, Any]) -> Dict[str, Any]:
    """Run the cross-stage evidence analysis on an ORFS reports dir (read-only).

    args: reports_dir (str, required), synth_stat_path (str, optional)
    """
    if _sta is None:
        return {"error": "stage_timing_analyzer unavailable"}
    rd = a.get("reports_dir", "")
    if not rd or not os.path.isdir(rd):
        return {"error": f"reports_dir not found: {rd}"}
    ev = _sta.analyze(rd, synth_stat_path=a.get("synth_stat_path"))
    rc = _sta.classify_root_cause(ev)
    return {
        "reports_dir": rd,
        "first_violation_stage": getattr(ev, "first_violation_stage", None),
        "timing_violated_pre_place": getattr(ev, "timing_violated_pre_place", None),
        "root_cause_layer": getattr(rc, "layer", None),
        "root_cause_confidence": getattr(rc, "confidence", None),
        "briefing": _sta.evidence_briefing(ev, rc),
    }


def t_fused_review(a: Dict[str, Any]) -> Dict[str, Any]:
    """Full adaptive verdict: fuse log signature with cross-stage evidence.

    This is the anchored, provenance-carrying verdict the agent's fail loop uses.
    args: error_text (req), block, reports_dir, synth_stat_path, stage_hint,
          proposed_fix_layer (optional -> also returns the guardrail verdict)
    """
    error_text = a.get("error_text", "")
    if not error_text:
        return {"error": "error_text is required"}
    block = a.get("block", "unknown")
    dep = {k: set(v) for k, v in DEFAULT_GRAPH.items()} or {block: set()}
    if block not in dep:
        dep[block] = set()
    fr = reasoning_engine.reason_with_reports(
        _split_lines(error_text), a.get("tail", ""), block, dep,
        report_dir=a.get("reports_dir"), synth_stat_path=a.get("synth_stat_path"),
        stage_hint=a.get("stage_hint"))
    out: Dict[str, Any] = {
        "block": block,
        "root_layer": fr.root_layer,
        "can_auto": fr.can_auto,
        "needs_llm": fr.needs_llm,
        "provenance": fr.provenance,
        "briefing": fr.briefing,
    }
    layer = a.get("proposed_fix_layer")
    if layer:
        allowed, why = fr.fix_allowed(layer)
        out["fix_allowed"] = {"layer": layer, "allowed": allowed, "reason": why}
    return out


def t_probe_stage(a: Dict[str, Any]) -> Dict[str, Any]:
    """Re-open a stage .odb READ-ONLY in OpenROAD and re-time it.

    Requires openroad on PATH; degrades to ok=False (never crashes) if absent.
    args: odb_path (req), group_count (int), timeout (int)
    """
    odb = a.get("odb_path", "")
    if not odb:
        return {"error": "odb_path is required"}
    r = orfs_probe.probe_stage_odb(
        odb, group_count=int(a.get("group_count", 5)),
        timeout=int(a.get("timeout", 300)))
    return {
        "ok": r.ok, "stage_odb": r.stage_odb, "wns": r.wns, "tns": r.tns,
        "design_area": r.design_area, "returncode": r.returncode,
        "error": r.error, "worst_paths_text": r.worst_paths_text[:2000],
    }


def t_locate_block_artifacts(a: Dict[str, Any]) -> Dict[str, Any]:
    """Locate the reports/ and results/ dirs for a block under an ORFS flow dir.

    args: flow_dir (req; the ORFS 'flow' directory), block (req)
    """
    flow_dir = a.get("flow_dir", "") or (
        os.path.join(os.environ["ORFS_DIR"], "flow") if os.environ.get("ORFS_DIR") else "")
    block = a.get("block", "")
    if not flow_dir or not block:
        return {"error": "flow_dir and block are required (or set ORFS_DIR)"}
    return {
        "flow_dir": flow_dir, "block": block,
        "reports_dir": _reports_dir_for(flow_dir, block),
        "results_dir": _results_dir_for(flow_dir, block),
    }


def t_trigger_build(a: Dict[str, Any]) -> Dict[str, Any]:
    """GUARDED action: build a block via janus_agent. DRY-RUN unless
    JANUS_MCP_ALLOW_ACTIONS=1 is set. Even when enabled, all real mutation is
    owned by janus_agent — this only assembles and (optionally) runs the command.

    args: blocks (str csv, req), extra_args (str, optional)
    """
    blocks = a.get("blocks", "")
    if not blocks:
        return {"error": "blocks is required (comma-separated)"}
    cmd = [sys.executable, os.path.join(_HERE, "janus_agent.py"), "--blocks", blocks]
    extra = a.get("extra_args", "")
    if extra:
        cmd += extra.split()
    allowed = os.environ.get("JANUS_MCP_ALLOW_ACTIONS") == "1"
    if not allowed:
        return {
            "dry_run": True,
            "would_run": " ".join(cmd),
            "note": ("actions disabled — set JANUS_MCP_ALLOW_ACTIONS=1 in the "
                     "server environment to actually launch this build."),
        }
    import subprocess
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=int(a.get("timeout", 60)))
        return {"dry_run": False, "ran": " ".join(cmd), "returncode": p.returncode,
                "stdout_tail": p.stdout[-4000:], "stderr_tail": p.stderr[-2000:]}
    except Exception as e:
        return {"dry_run": False, "ran": " ".join(cmd), "error": str(e)}


# --------------------------------------------------------------------------- #
# Tool registry — the single source of truth for BOTH front-ends.
# schema follows JSON-Schema (MCP inputSchema).
# --------------------------------------------------------------------------- #
def _obj(props: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {"type": "object", "properties": props, "required": required}


_STR = {"type": "string"}
_INT = {"type": "integer"}

TOOLS: Dict[str, Tuple[str, Dict[str, Any], Callable[[Dict[str, Any]], Dict[str, Any]]]] = {
    "list_blocks": (
        "List the JANUS block dependency graph and a valid build order.",
        _obj({}, []), t_list_blocks),
    "reason_about_failure": (
        "Deterministically classify an EDA failure from log text alone: ranked "
        "root-cause layer hypotheses, blast radius, and a staff-engineer briefing.",
        _obj({"error_text": _STR, "block": _STR, "stage_hint": _STR, "tail": _STR},
             ["error_text"]), t_reason_about_failure),
    "analyze_reports": (
        "Cross-stage evidence analysis over an ORFS reports dir (read-only): "
        "where timing first broke and the deterministic root-cause layer.",
        _obj({"reports_dir": _STR, "synth_stat_path": _STR}, ["reports_dir"]),
        t_analyze_reports),
    "fused_review": (
        "Full adaptive verdict: fuse the log signature with cross-stage evidence, "
        "anchored on the failing-stage fact, with provenance and an optional "
        "guardrail check on a proposed fix layer.",
        _obj({"error_text": _STR, "block": _STR, "reports_dir": _STR,
              "synth_stat_path": _STR, "stage_hint": _STR,
              "proposed_fix_layer": _STR, "tail": _STR}, ["error_text"]),
        t_fused_review),
    "probe_stage": (
        "Re-open a stage .odb READ-ONLY in OpenROAD and re-time it (WNS/TNS/area). "
        "Requires openroad on PATH; degrades gracefully if absent.",
        _obj({"odb_path": _STR, "group_count": _INT, "timeout": _INT}, ["odb_path"]),
        t_probe_stage),
    "locate_block_artifacts": (
        "Locate the reports/ and results/ dirs for a block under an ORFS flow dir.",
        _obj({"flow_dir": _STR, "block": _STR}, ["block"]),
        t_locate_block_artifacts),
    "trigger_build": (
        "GUARDED: build a block via janus_agent. Dry-run unless "
        "JANUS_MCP_ALLOW_ACTIONS=1 is set in the server environment.",
        _obj({"blocks": _STR, "extra_args": _STR, "timeout": _INT}, ["blocks"]),
        t_trigger_build),
}


def dispatch(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Call a tool by name. Never raises — errors come back as {'error': ...}."""
    entry = TOOLS.get(name)
    if entry is None:
        return {"error": f"unknown tool: {name}"}
    _, _, handler = entry
    try:
        return handler(arguments or {})
    except Exception as e:  # keep the transport alive on any handler bug
        return {"error": f"{type(e).__name__}: {e}"}


def tool_catalogue() -> List[Dict[str, Any]]:
    return [{"name": n, "description": desc, "inputSchema": schema}
            for n, (desc, schema, _) in TOOLS.items()]


# --------------------------------------------------------------------------- #
# Front-end A: official MCP SDK (preferred when installed).
# --------------------------------------------------------------------------- #
def _run_with_sdk() -> bool:
    try:
        from mcp.server.fastmcp import FastMCP
    except Exception:
        return False

    app = FastMCP(SERVER_NAME)

    # Register each tool as a thin wrapper that forwards to dispatch(). We build
    # closures so the SDK sees a proper signature-less **kwargs tool.
    def _make(tool_name: str):
        def _fn(**kwargs) -> str:
            return json.dumps(dispatch(tool_name, kwargs), indent=2, default=str)
        return _fn

    for name, (desc, _schema, _h) in TOOLS.items():
        app.add_tool(_make(name), name=name, description=desc)

    app.run()  # stdio
    return True


# --------------------------------------------------------------------------- #
# Front-end B: self-contained stdlib JSON-RPC 2.0 over stdio (no dependency).
# Implements the MCP core: initialize, tools/list, tools/call, plus ping.
# --------------------------------------------------------------------------- #
def _rpc_result(msg_id: Any, result: Any) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result})


def _rpc_error(msg_id: Any, code: int, message: str) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": msg_id,
                       "error": {"code": code, "message": message}})


def _handle_rpc(req: Dict[str, Any]) -> Optional[str]:
    """Return a JSON string to write back, or None for notifications."""
    method = req.get("method")
    msg_id = req.get("id")
    params = req.get("params") or {}

    if method == "initialize":
        return _rpc_result(msg_id, {
            "protocolVersion": params.get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    if method in ("notifications/initialized", "initialized"):
        return None  # notification, no response
    if method == "ping":
        return _rpc_result(msg_id, {})
    if method == "tools/list":
        return _rpc_result(msg_id, {"tools": tool_catalogue()})
    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments") or {}
        result = dispatch(name, args)
        # MCP content envelope: a single text block with the JSON payload.
        return _rpc_result(msg_id, {
            "content": [{"type": "text",
                         "text": json.dumps(result, indent=2, default=str)}],
            "isError": bool(isinstance(result, dict) and result.get("error")),
        })
    if msg_id is not None:
        return _rpc_error(msg_id, -32601, f"method not found: {method}")
    return None


def _run_stdlib_stdio() -> None:
    """Minimal line-delimited JSON-RPC loop over stdio.

    MCP stdio framing is newline-delimited JSON objects (one per line). This is
    what the reference clients emit; we read a line, parse, respond with a line.
    """
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            out.write(_rpc_error(None, -32700, "parse error") + "\n")
            out.flush()
            continue
        resp = _handle_rpc(req)
        if resp is not None:
            out.write(resp + "\n")
            out.flush()


# --------------------------------------------------------------------------- #
# Offline self-test — exercises the read-only tools with no mcp/openroad.
# --------------------------------------------------------------------------- #
def _selftest() -> int:
    fails = 0

    def check(cond: bool, msg: str) -> None:
        nonlocal fails
        print(("  ok: " if cond else "  FAIL: ") + msg)
        if not cond:
            fails += 1

    lb = dispatch("list_blocks", {})
    check("janus_soc_top" in lb["blocks"], "list_blocks returns the graph")
    check(lb["build_order"].index("security") < lb["build_order"].index("janus_soc_top"),
          "build_order is topologically sane (security before soc_top)")

    # the exact PDN-0179 mis-blame case: must classify as FLOORPLAN, not RTL.
    rf = dispatch("reason_about_failure", {
        "error_text": "[ERROR PDN-0179] Unable to repair all channels.",
        "block": "user_core_cva6"})
    check(rf["top"]["layer"] == "FLOORPLAN", "PDN-0179 classified FLOORPLAN via MCP")
    check(rf["top"]["signature"] == "pdn_repair_channels", "correct signature via MCP")

    fr = dispatch("fused_review", {
        "error_text": "[ERROR PDN-0179] Unable to repair all channels.",
        "block": "user_core_cva6"})
    check(fr["root_layer"] == "FLOORPLAN", "fused_review anchors FLOORPLAN via MCP")
    check("PROVENANCE" in fr["provenance"], "fused_review carries provenance via MCP")

    # unknown tool + missing-arg paths degrade, never raise.
    check("error" in dispatch("nope", {}), "unknown tool returns error, no crash")
    check("error" in dispatch("reason_about_failure", {}), "missing arg returns error")

    # probe_stage with a bad path degrades to ok=False.
    ps = dispatch("probe_stage", {"odb_path": "/nonexistent/x.odb"})
    check(ps["ok"] is False, "probe_stage degrades on missing odb")

    # guarded action is dry-run by default.
    tb = dispatch("trigger_build", {"blocks": "security"})
    check(tb.get("dry_run") is True, "trigger_build is dry-run without opt-in")

    # RPC envelope smoke test.
    resp = json.loads(_handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}))
    check(len(resp["result"]["tools"]) == len(TOOLS), "tools/list returns full catalogue")
    call = json.loads(_handle_rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                   "params": {"name": "list_blocks", "arguments": {}}}))
    check(call["result"]["content"][0]["type"] == "text", "tools/call wraps JSON in text block")

    print(f"\nmcp_server self-test: {'ALL PASSED' if not fails else str(fails)+' FAILED'}")
    return 1 if fails else 0


def main(argv: List[str]) -> int:
    if "--list-tools" in argv:
        print(json.dumps(tool_catalogue(), indent=2))
        return 0
    if "--selftest" in argv:
        return _selftest()
    # Prefer the official SDK; fall back to the built-in stdlib server.
    if not _run_with_sdk():
        _run_stdlib_stdio()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
