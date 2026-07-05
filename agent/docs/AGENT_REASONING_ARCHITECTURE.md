# JANUS Agent — Reasoning Architecture

> Goal: make `janus_agent.py` reason about failures like a **staff/principal
> engineer** across the *whole* RTL→GDS flow — deterministically where the
> physics is known, and with a *precisely framed* LLM consult where it isn't —
> instead of pattern-matching a log and applying a canned fix.

---

## 1. Why can the assistant reason but the agent can't — they call the *same* LLM?

This is the crux question, and the honest answer is: **the model is not the
bottleneck — the scaffolding is.**

When the human-facing assistant debugged the MPL-0006 macro-fit issue, it did
*not* do it in one LLM call. It ran a **loop**:

| The assistant's loop | What `janus_agent.py` did (before) |
|---|---|
| Read arbitrary files (LEF, RTL, configs, docs) | Sent a fixed ~4 KB log excerpt |
| Formed **multiple** hypotheses, ranked them | One-shot: "here is a log, return a fix" |
| Ran read-only analysis (measured LEF `SIZE`, computed aspect ratios) | No tool access |
| Reasoned about **cross-stage impact** ("if I change utilization, does it hide an RTL bug?") | No blast-radius model |
| Tested a hypothesis, observed, **iterated** | Apply once, retry, rollback if worse |
| Decided *which layer* owned the fix (RTL vs floorplan vs tool) | Constrained JSON fix schema, no layer decision |

Same weights, wildly different **context, tools, iteration, and framing**. The
LLM inside the agent was in a straitjacket: no retrieval, no analysis tools, no
hypothesis ranking, no notion of what a fix costs downstream.

**So the fix is not "a smarter model" — it is to give the agent the assistant's
scaffolding:** evidence gathering, deterministic analysis, hypothesis ranking,
blast-radius reasoning, and a framed decision ("which *layer* owns this?") before
the LLM is ever consulted. `physical_analyzer.py` already proved one slice of
this works. This architecture generalises it.

---

## 2. Design principles

1. **Determinism first.** If the physics/rule is known, decide without an LLM.
   Credits (and latency, and non-determinism) are spent only on genuinely novel
   or judgement-call failures.
2. **Tool-agnostic core, tool-specific edges.** *Which layer owns the fix* and
   *what it costs downstream* are independent of ORFS/Yosys/OpenROAD. Only the
   concrete workaround (a Makefile var, a yosys pass, an SDC line) is
   tool-specific — and that lives in `fix_applier.py`, not the reasoning core.
3. **Blast-radius awareness.** Every candidate fix is scored by which flow
   *stages* and which dependent *blocks* it invalidates. The agent is pushed to
   the **smallest reversible** change that fixes the **root cause** — never a
   band-aid (e.g. lowering `CORE_UTILIZATION` to dodge a real RTL area blowup).
4. **Tool-bug triage.** Before proposing any design change, decide whether the
   *tool* crashed (segfault, missing `.so`, internal assertion). If so the remedy
   is a workaround / skip / version-pin, not an RTL/SDC edit.
5. **Confidence-gated autonomy.** High-confidence + deterministic + low-blast +
   reversible ⇒ auto-apply. Otherwise stop (interactive) or consult the LLM with
   a framed briefing. Never auto-apply a high-blast-radius change silently.

---

## 3. The remedy-layer taxonomy (tool-agnostic)

Every failure's root cause is attributed to exactly one **layer**, and each layer
has a deterministic **blast radius** (which stages must re-run):

| Layer | Owns | Re-runs from |
|---|---|---|
| `ENV` | host/packaging (missing `.so`, OOM, PATH) | failing stage |
| `TOOL_WA` | the EDA tool is buggy → workaround/skip/pin | failing stage |
| `RTL` | the source hardware is wrong / too big | `1_synth` (⇒ everything) |
| `SDC` | timing constraints | `1_synth` |
| `SYNTH_OPT` | synthesis options (effort/ABC/flatten/retime) | `1_synth` |
| `FLOORPLAN` | die/core area, utilization, macro place, PDN | `2_floorplan` |
| `PLACE` | density / padding / legalization | `3_place` |
| `CTS` | clock tree options | `4_cts` |
| `ROUTE` | routing layers, congestion, antenna, DRC | `5_route` |

**Cross-block ripple:** an `RTL` / area-moving `FLOORPLAN` / `SYNTH_OPT` change to
a block alters its `.lef/.lib` abstract, so every block that consumes it as a
macro (per `DEFAULT_GRAPH`) must also re-run. `SDC`/`CTS`/`ROUTE`/tool fixes that
preserve the footprint do not ripple. This is computed transitively.

Implemented in `reasoning_engine.py`: `Layer`, `downstream_stages()`,
`affected_blocks()`.

---

## 4. Component architecture (a "virtual design team")

```
                         ┌─────────────────────────────┐
   ORFS make fails ─────▶│  Evidence Builder            │  logs, reports, RTL,
                         │  (log_analyzer + retrieval)  │  SDC, configs, prior fixes
                         └──────────────┬──────────────┘
                                        ▼
                         ┌─────────────────────────────┐
                         │  Deterministic Triage        │  env/infra? (classify_failure)
                         │  (log_analyzer.classify)     │  → precise manual msg, NO LLM
                         └──────────────┬──────────────┘
                                        ▼
                         ┌─────────────────────────────┐   ◀── THE BRAIN (new)
                         │  Reasoning Engine            │   • signature catalogue
                         │  reasoning_engine.reason()   │   • root-cause → layer
                         │                              │   • blast radius (stage+block)
                         │                              │   • tool-bug triage
                         └───────┬──────────────┬───────┘
              deterministic &    │              │  novel / low-confidence /
              high-confidence    │              │  judgement call
                                 ▼              ▼
              ┌────────────────────────┐   ┌─────────────────────────────┐
              │ Specialist analyzers   │   │  LLM consult (framed)        │
              │ physical_analyzer (MPL)│   │  llm_interface.request_fix(  │
              │ [future: timing/cong.] │   │     physical=…, briefing=…)  │
              └───────────┬────────────┘   └──────────────┬──────────────┘
                          │                                │
                          ▼                                ▼
                         ┌─────────────────────────────────────┐
                         │  Fix Applier + Verifier             │  apply → re-run stage →
                         │  fix_applier.apply(); rollback      │  better? keep : revert
                         └──────────────┬──────────────────────┘
                                        ▼
                         ┌─────────────────────────────┐
                         │  Publisher (git_ops)         │  commit + dual-branch push
                         └─────────────────────────────┘
```

### Existing modules and their role
- `log_analyzer.py` — evidence + env/infra triage (already deterministic).
- `physical_analyzer.py` — the first specialist analyzer (macro-fit, deterministic).
- `reasoning_engine.py` — **new**: the tool-agnostic brain (this doc's subject).
- `llm_interface.py` — the framed LLM consult (now receives a briefing block).
- `fix_applier.py` — tool-specific application + rollback.
- `git_ops.py` — commit + dual-branch (`main` + `v5.1_integration`) publish.

---

## 5. Phased roadmap

- **Phase 0 (done):** env triage (`classify_failure`) + one specialist
  (`physical_analyzer`, MPL-0006) + dual-branch push.
- **Phase 1 (this change):** `reasoning_engine.py` — stage-aware signature
  catalogue, layer taxonomy, blast-radius (stage + cross-block), tool-bug
  triage, and a framed "principal-engineer" briefing for the LLM. Opt-in,
  self-tested, no behaviour change unless enabled.
- **Phase 2 (next):** wire `reason()` into the fail loop between triage and the
  LLM; feed `briefing` into `request_fix()`; add a **verifier** that re-runs only
  the blast-radius stages and rolls back if no improvement.
- **Phase 3:** more specialist analyzers so more failures are deterministic —
  a **timing** analyzer (parse OpenSTA slack reports, decide SDC vs SYNTH_OPT vs
  CTS) and a **congestion** analyzer (utilization ↔ congestion heuristic).
- **Phase 4:** proposer/critic **debate** for high-blast-radius, low-confidence
  cases: two LLM personas (a domain specialist proposes, a reviewer challenges),
  reconciled by the engine; auto-apply only above a confidence threshold and
  below a blast-radius threshold, else escalate to the human.
- **Phase 5:** persistent **knowledge base** (failure→fix→outcome). Repeated
  failures graduate from LLM judgement to deterministic rules over time.

---

## 6. Honesty caveats
- FakeRAM macros are abstract (floorplan/rough-PPA only) — reshapes preserve
  bitcell count and pins but are not silicon-signoff geometry.
- The reasoning engine's confidences are heuristics for *classification*, not
  guarantees the fix is correct. High-blast-radius changes should stay
  human-gated until the Phase-2 verifier is in place.
- Timing/congestion reasoning is still LLM-assisted until the Phase-3 analyzers
  land; treat those decisions as advisory.
