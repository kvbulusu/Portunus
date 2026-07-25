# JANUS ASIC — MCP server (`mcp_server.py`)

A **Model Context Protocol** surface over the agent's deterministic, read-only
investigators (`reasoning_engine`, `stage_timing_analyzer`, `orfs_probe`).
Item 1 of the vibe-ic integration (see `docs/VIBEIC_EDA_EVALUATION.md` §8).

## Why

The agent's value is the *scaffolding* around the model, not the model itself.
Exposing that scaffolding over MCP lets **any** MCP client — and therefore **any
model you choose, open-source included** — ask the same precisely-framed,
evidence-backed engineering questions the agent's own fail loop asks. MCP is a
wire protocol, so the model is interchangeable; nothing here is vendor-locked.

## Zero dependency

- If the official `mcp` Python SDK is installed, the server uses it (preferred).
- If not, it falls back to a **self-contained stdlib JSON-RPC 2.0 stdio loop**
  implementing the MCP core (`initialize`, `tools/list`, `tools/call`). No pip
  install, no network — matching the rest of the agent.

```bash
python3 mcp_server.py --list-tools   # print the tool catalogue
python3 mcp_server.py --selftest      # exercise the read-only tools offline
python3 mcp_server.py                 # run as a stdio MCP server
```

## Tools (read-only by default)

| tool | what it does |
|------|--------------|
| `list_blocks` | JANUS block dependency graph + a valid build order |
| `reason_about_failure` | classify a failure from log text alone → ranked root-cause **layer**, blast radius, staff-engineer briefing |
| `analyze_reports` | cross-stage evidence over an ORFS `reports/` dir → where timing first broke + root-cause layer |
| `fused_review` | the anchored verdict: log signature **fused** with cross-stage evidence, with **provenance** + optional guardrail check on a proposed fix layer |
| `probe_stage` | re-open a stage `.odb` **read-only** in OpenROAD and re-time it (WNS/TNS/area); degrades gracefully if `openroad` is absent |
| `locate_block_artifacts` | find the `reports/` and `results/` dirs for a block under an ORFS flow dir |

### Guarded action (off by default)

| tool | behaviour |
|------|-----------|
| `trigger_build` | build a block via `janus_agent`. **Dry-run** (returns the command it *would* run) unless `JANUS_MCP_ALLOW_ACTIONS=1` is set in the server's environment. All real mutation stays owned by `janus_agent`. |

## Wiring it to a client

The server speaks MCP over **stdio**. Point any MCP-capable client at it.

**Generic MCP client config** (Claude Desktop, Continue, Cline, etc. all use this
shape):

```json
{
  "mcpServers": {
    "janus-asic": {
      "command": "python3",
      "args": ["/abs/path/to/asic_flow/agent/mcp_server.py"],
      "env": { "ORFS_DIR": "/home/you/OpenROAD-flow-scripts" }
    }
  }
}
```

**Open-source models:** run your model behind any MCP-capable client (e.g. the
`mcp` CLI, LM Studio, Continue with a local Ollama/vLLM backend) and add the
block above. The server does not care which model calls it.

To enable the guarded build action, add `"JANUS_MCP_ALLOW_ACTIONS": "1"` to `env`.

## Guarantees

- **Deterministic.** Same inputs → same outputs; provenance carries a SHA-1 of
  the exact deciding log line.
- **Non-mutating by default.** Every default tool only reads; `probe_stage` opens
  the `.odb` read-only and never writes it back.
- **Crash-safe transport.** A handler bug returns `{"error": ...}`; it never
  takes the server down.
