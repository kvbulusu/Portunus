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

## 5a. The collaborative two-agent model (design-intent agent ⇄ tool agent)

Your vision — a **design agent** that understands the JANUS architecture / SDC /
RTL / configs, and a **tool agent** that understands the EDA tool — is exactly
the right decomposition. It maps onto the taxonomy above:

```
   ┌───────────────────────────────┐        ┌───────────────────────────────┐
   │  DESIGN-INTENT AGENT           │        │  TOOL AGENT                    │
   │  owns: RTL, SDC, SYNTH_OPT,    │◀──────▶│  owns: ENV, TOOL_WA, and the   │
   │        FLOORPLAN, PLACE, CTS,  │ debate │        tool-specific *form* of  │
   │        ROUTE  (design intent)  │        │        every workaround         │
   │  knows: janus_pkg params, addr │        │  knows: ORFS/Yosys/OpenROAD    │
   │  map, clock domains, macro     │        │  option names, known bugs,     │
   │  geometry, dependency graph    │        │  version quirks, .so/pkg layout│
   └───────────────┬───────────────┘        └───────────────┬───────────────┘
                   └──────────────┬──────────────────────────┘
                                  ▼
                   ┌───────────────────────────────┐
                   │  RECONCILER (reasoning_engine) │  deterministic referee:
                   │  • is this a tool bug or a     │  arbitrates the two views,
                   │    design bug? (tool-bug triage)│  scores by blast radius,
                   │  • which layer owns the fix?   │  requires agreement + a
                   │  • blast radius + reversibility│  confidence/blast gate
                   │  • confidence gate → apply /   │  before ANY apply.
                   │    escalate to human           │
                   └───────────────────────────────┘
```

**Division of labour, made concrete on failures we have already seen:**

| Failure | Design agent says | Tool agent says | Reconciler decision |
|---|---|---|---|
| MPL-0006 macro-fit | "SRAM aspect ratio is pathological — reshape banks; bitcells/pins unchanged" | "FakeRAM2.0 `banks`/`column_mux_factor` are the knobs; geometry is `KH·depth/F` × `KW·W·F`" | deterministic reshape (geometry-exact) → **auto-apply** |
| `libnaja*.so` at CTS | "design is fine; equiv-check is optional" | "loader path bug; prepend dir or `EQUIVALENCE_CHECK=0`" | TOOL_WA → **auto** (already done at preflight) |
| synth cell-count blowup | "an RTL structure exploded — fix the RTL" | "not a synth-option issue; ABC/flatten won't help" | RTL, high blast radius → **escalate to human** |
| segfault in router | "not a design bug" | "tool defect; try alt option/order or pin version" | TOOL_WA, low confidence → **framed LLM consult** |
| WNS < 0 | "is the SDC realistic? which path? arch too long?" | "retime/effort knobs exist but won't fix a real arch path" | SDC vs SYNTH_OPT → **framed LLM consult** |

Today both "agents" are the **same LLM** invoked with different framing plus the
deterministic reconciler (`reasoning_engine.py`). The **Phase-4 debate** upgrade
splits them into two explicit personas (proposer/critic) that must *agree*
before the reconciler will act — with the engine enforcing the confidence and
blast-radius gates so a disagreement or a high-blast change always escalates to
you rather than being auto-applied.

## 5b. Determinism & anti-speculation guarantees (what can auto-apply)

Because a bad fix can corrupt the design, autonomy is **strictly gated**:

1. **Reasoning is advisory.** `reasoning_engine.reason()` NEVER edits a file. It
   only classifies, computes blast radius, and frames the consult. Proven by
   `tests/test_reasoning_loop.py` (RTL/timing/segfault/novel failures produce
   **zero** commits and zero file edits).
2. **The ONLY deterministic auto-apply is the macro-fit reshape** — and only
   because its effect is *geometry-exact and reversible* (bitcell count and pin
   interface are provably unchanged; it is a re-fold, not a design change).
3. **Everything else is gated**: env/tool issues → precise manual instruction;
   design-layer fixes → LLM consult that is itself gated behind `--interactive`
   approval, and any applied fix is committed (never force-pushed) so you can
   `git revert`. High-blast-radius changes are surfaced, not silently applied.
4. **`--reason` changes nothing that gets applied** — it only enriches the
   diagnosis and the LLM framing. `--reason` OFF reproduces the prior behaviour
   exactly (verified).

## 5c. Validation performed for this change

- `reasoning_engine.py` self-test: 6/6 (detection, layer, blast radius,
  cross-block ripple, tool-bug, novel).
- `tests/test_reasoning_loop.py`: drives the **real** `_process_block` fail loop
  with **real ORFS/Yosys/OpenROAD log text**, mocking only the `make` subprocess
  and the git/LLM edges. 7/7 scenarios, including the safety property above.
- **ORFS in this sandbox:** a full OpenROAD+ASAP7 flow was **not** run here — the
  container has the docker *client* but no daemon/engine, no apt/conda/prebuilt
  OpenROAD, and a from-source build is multi-hour and fragile. Crucially, a
  *passing* block run would exercise **none** of this code (the reasoning/
  self-heal path only fires on failures), so failure-log replay against the real
  code path is the higher-fidelity validation. A real `noc`/`security` run
  should still be done on your WSL2 install with `--reason` to confirm on live
  tool output.

## 6. Honesty caveats
- FakeRAM macros are abstract (floorplan/rough-PPA only) — reshapes preserve
  bitcell count and pins but are not silicon-signoff geometry.
- The reasoning engine's confidences are heuristics for *classification*, not
  guarantees the fix is correct. High-blast-radius changes should stay
  human-gated until the Phase-2 verifier is in place.
- Timing/congestion reasoning is still LLM-assisted until the Phase-3 analyzers
  land; treat those decisions as advisory.
