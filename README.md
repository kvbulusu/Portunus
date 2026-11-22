# Portunus

**An auditable AI agent for RTL-to-GDS timing closure on OpenROAD.**

Portunus is the Roman god of keys, doors and harbors. The name fits a tool whose
job is to get a design through the last gate before signoff. It was the
companion of Janus, and this agent was built while closing the
**JANUS** RISC-V SoC.

> **Status: v0.1, extracted from a working project.** The code here drove the
> full hierarchical physical implementation of JANUS (ASAP7, OpenROAD-flow-scripts,
> 8 hardened blocks plus top level). It has not yet been made design-agnostic: block
> names, dependency waves and some fixes are still JANUS-specific. See the roadmap.

## What it does
- **Runs ORFS block by block in dependency order**, with resume and parallel
  waves, and generates `.lef`/`.lib` abstracts for hierarchical builds.
- **Diagnoses failures and timing deterministically first.** It reads logs and
  reports, classifies violating paths (setup/hold, clock-tree, I/O, macro, slew)
  and picks a fix strategy with a rule-based reasoning engine. An LLM is called
  only as a last resort, as a single bounded consultation.
- **Applies fixes safely.** Every change goes through a fix applier with
  rollback, is committed with a reason, and is judged against a pre-fix
  baseline. A change that makes QoR worse is reverted.
- **Adaptive closure and ECOs.** Two-pass CTS/SPEF closure, hold-only ECOs,
  drv/slew ECOs, and RCX-based signoff STA.
- **Reporting.** Stage reports, QoR dashboard, report bundles and optional email
  and Google Drive notifications.
- **MCP server.** Exposes read-only flow tools (status, timing, logs) to MCP clients.

## Layout
```
agent/   janus_agent.py (CLI), reasoning_engine, log_analyzer, timing_classifier,
         fix_applier, adaptive_closure, incremental_opt, mcp_server, dashboard, tests/
utils/   flow_state, stage_report, report_bundle, eco_history, notification, ...
```
The agent expects to sit inside a flow root with `configs/` and `scripts/`
(as in `janus-ai-soc/asic_flow/`). Making that root configurable is the first
roadmap item.

## Tests
```bash
python3 -m pytest -q agent/tests      # 194 standalone tests
```
Another 86 test files exercise the agent against the JANUS reference design
(configs, SDC, Tcl hooks). They are listed in `agent/tests/reference_design_tests.txt`
and run only when a reference design tree is present.

## Results it produced (JANUS v1, ASAP7)
3.54 mm² die, about 3.7 M gate equivalents and 551 SRAM macros. The 350 MHz user
domain closes at about 343 MHz (one path −59 ps). Hold is clean and DRC is 0.
ASAP7 is a predictive academic PDK, so these are not silicon results.

## Roadmap
1. Configurable flow root and block graph (no hard-coded JANUS blocks).
2. Run on public ORFS designs (gcd, aes, ibex, jpeg) as a generality check.
3. A benchmark of real closure problems and fixes, with pass/fail scoring.
4. Provider-neutral LLM interface and a tool-use loop with trajectory logging.
5. Packaging (`pip install portunus`).

## License
Apache-2.0. See `LICENSE`.
