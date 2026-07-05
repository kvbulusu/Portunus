#!/usr/bin/env python3
"""
test_reasoning_loop.py — end-to-end validation of the Phase-2 reasoning wiring
by driving the REAL JANUSAutonomousAgent._process_block() fail loop with REAL
ORFS / Yosys / OpenROAD failure log text.

We mock ONLY:
  * _run_make      -> writes a chosen real log excerpt + returns a chosen rc
                      (so we need no OpenROAD/ORFS toolchain), and
  * agent.git      -> a recorder (so nothing is pushed) , and
  * agent.llm      -> a recorder (so ZERO credits are spent).

Everything else is the real code: log_analyzer, classify_failure (triage),
reasoning_engine.reason(), the deterministic macro-fit self-heal
(physical_analyzer + fix_applier), the LLM gating, and the publish path.

The point is to prove the SAFETY property the design demands:
  reasoning is ADVISORY — it NEVER auto-applies a speculative change.
  The ONLY deterministic auto-apply is the geometry-exact macro-fit reshape.
"""
import argparse
import os
import shutil
import sys
import tempfile
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_AGENT = os.path.dirname(_HERE)
sys.path.insert(0, _AGENT)

import janus_agent as JA  # noqa: E402


# --- real captured log excerpts (trimmed) -----------------------------------
LOGS = {
    "macro_fit": (
        "[INFO GPL-0002] DBU: 1000\n"
        "[INFO MPL-0001] Reading macro placement\n"
        "[ERROR MPL-0006] Found macro u_kv_cache/u_spm/g_bank[0]."
        "g_deep.u_macro/u_ram that does not fit in the core.\n"
        "Error: RTL-MP failed.\n"
    ),
    "env_missing_so": (
        "[INFO] Running cts.tcl\n"
        "kepler-formal: error while loading shared libraries: "
        "libnaja_snl_pyloader.so: cannot open shared object file: "
        "No such file or directory\n"
        "make: *** [cts] Error 127\n"
    ),
    "rtl_blowup": (
        "[INFO] Executing SYNTH pass.\n"
        "[WARNING] design has 12983440 cells; netlist too large for target\n"
        "Error: synthesis produced an unroutable netlist.\n"
    ),
    "timing": (
        "[INFO] Detailed routing done.\n"
        "worst negative slack -0.184\n"
        "[ERROR] timing not met: setup violation on 42 endpoints\n"
    ),
    "segfault": (
        "[INFO] global_route start\n"
        "Segmentation fault (core dumped)\n"
        "make: *** [route] Error 139\n"
    ),
    "novel": (
        "[INFO] doing something\n"
        "Error: unexpected frobnicator overflow 0xdeadbeef in widget stage\n"
    ),
    "pass": "[INFO] flow completed.\n[INFO] no errors.\nDone.\n",
}


class FakeGit:
    def __init__(self):
        self.commits = []
        self.pushes = 0
        self.sync_branches = ["main", "v5.1_integration"]
    def available(self): return True
    def current_branch(self): return "main"
    def commit_paths(self, paths, msg):
        self.commits.append((list(paths), msg))
        return True, f"committed {len(paths)} file(s)"
    def push(self):
        self.pushes += 1
        return True, "pushed HEAD -> main✓, v5.1_integration✓"


class FakeLLMResult:
    def __init__(self):
        self.ok = False; self.fixes = []; self.error = "no fix (test)"
        self.diagnosis = "test-no-fix"; self.confidence = "low"
        self.chars_in = 0; self.chars_out = 0


class FakeLLM:
    """Records request_fix kwargs; returns no actionable fix so the loop ends
    without spending credits."""
    def __init__(self):
        self.calls = []
    def available(self): return True
    def request_fix(self, excerpt, physical="", reasoning=""):
        self.calls.append({"physical": physical, "reasoning": reasoning})
        return FakeLLMResult()
    def usage(self): return {"llm_calls": len(self.calls), "approx_tokens": 0}


def make_args(**over):
    d = dict(
        blocks=None, parallel=1, max_retries=2, orfs="", rtl_root=None,
        run_dir=None, resume=False, dry_run=False, estimate=False,
        no_llm=False, interactive=False, no_git_commit=False, git_push=False,
        git_sync_branches=["main", "v5.1_integration"], reason=True,
        no_physical=False, no_sram_regen=True, no_abstract=True,
        llm_interval=0.0, llm_retries=1, verbose=False, heartbeat=999,
    )
    d.update(over)
    return argparse.Namespace(**d)


def build_agent(tmp, **over):
    rtl_root = os.path.join(tmp, "repo"); os.makedirs(rtl_root, exist_ok=True)
    args = make_args(rtl_root=rtl_root, run_dir=os.path.join(tmp, "run"), **over)
    ag = JA.JANUSAutonomousAgent(args)
    ag.git = FakeGit()
    ag.llm = FakeLLM()
    # point the SRAM setup script at a throwaway copy so a reshape never
    # touches the real tracked script, and never regenerates macros.
    real = os.path.join(_AGENT, "..", "scripts", "04_setup_srams.sh")
    if os.path.isfile(real):
        dst = os.path.join(tmp, "04_setup_srams.sh")
        shutil.copy(real, dst)
        ag.sram_setup_script = dst
    ag.sram_out_dir = os.path.join(tmp, "srams")  # empty -> model fallback
    return ag


def drive(ag, block, fail_log, pass_after=None):
    """Replace _run_make so attempt N writes fail_log (rc=1); if pass_after is
    set, attempts >= pass_after write the pass log (rc=0)."""
    state = {"n": 0}
    def fake_run_make(b):
        state["n"] += 1
        log = LOGS["pass"] if (pass_after and state["n"] >= pass_after) else fail_log
        with open(ag._block_log(b), "w") as fh:
            fh.write(log)
        return 0 if (pass_after and state["n"] >= pass_after) else 1
    ag._run_make = fake_run_make
    return ag._process_block(block)


# ---------------------------------------------------------------------------- #
def main():
    passed = []

    # 1) MACRO-FIT: deterministic reshape auto-applies (NO LLM), commits once.
    with tempfile.TemporaryDirectory() as tmp:
        ag = build_agent(tmp)
        res = drive(ag, "security", LOGS["macro_fit"], pass_after=2)
        assert res.status == "DONE", res.status
        assert len(ag.git.commits) == 1, ("macro-fit should commit exactly once",
                                          ag.git.commits)
        assert len(ag.llm.calls) == 0, ("macro-fit must NOT call the LLM",
                                        ag.llm.calls)
        # committed file is the SRAM setup script (a reshape), nothing else
        paths = ag.git.commits[0][0]
        assert any("04_setup_srams.sh" in p for p in paths), paths
        passed.append("macro_fit: deterministic reshape, 0 LLM, 1 commit")

    # 2) ENV missing .so: triaged BEFORE reasoning/LLM -> NEEDS_MANUAL, no writes
    with tempfile.TemporaryDirectory() as tmp:
        ag = build_agent(tmp)
        res = drive(ag, "noc", LOGS["env_missing_so"])
        assert res.status == "NEEDS_MANUAL", res.status
        assert len(ag.llm.calls) == 0 and len(ag.git.commits) == 0
        passed.append("env: triaged, 0 LLM, 0 commit")

    # 3) RTL blowup + --no-llm: reasoning classifies + diagnoses, applies NOTHING
    with tempfile.TemporaryDirectory() as tmp:
        ag = build_agent(tmp, no_llm=True)
        res = drive(ag, "security", LOGS["rtl_blowup"])
        assert res.status == "FAILED", res.status
        assert len(ag.git.commits) == 0, ("SAFETY: RTL failure must NOT auto-edit",
                                          ag.git.commits)
        assert len(ag.llm.calls) == 0, "no LLM under --no-llm"
        assert "reason:RTL" in res.note, res.note   # classification surfaced
        passed.append("rtl_blowup: classified RTL, 0 auto-edit, diagnosis in note")

    # 4) TIMING with LLM enabled: reasoning briefing is injected into request_fix
    with tempfile.TemporaryDirectory() as tmp:
        ag = build_agent(tmp)
        res = drive(ag, "user_domain", LOGS["timing"])
        assert len(ag.llm.calls) == 1, ag.llm.calls
        br = ag.llm.calls[0]["reasoning"]
        assert "PRINCIPAL-ENGINEER" in br and "blast radius" in br.lower(), br[:200]
        assert len(ag.git.commits) == 0, "no fix returned -> nothing committed"
        passed.append("timing: LLM consulted WITH reasoning briefing injected")

    # 5) SEGFAULT: tool-bug suspected is surfaced (advisory), no auto-edit
    with tempfile.TemporaryDirectory() as tmp:
        ag = build_agent(tmp, no_llm=True)
        res = drive(ag, "noc", LOGS["segfault"])
        # segfault may be caught by triage OR reasoning; either way: no auto-edit
        assert len(ag.git.commits) == 0
        passed.append(f"segfault: no auto-edit (status={res.status})")

    # 6) NOVEL failure + --no-llm: no signature, still no speculative edit
    with tempfile.TemporaryDirectory() as tmp:
        ag = build_agent(tmp, no_llm=True)
        res = drive(ag, "noc", LOGS["novel"])
        assert res.status == "FAILED"
        assert len(ag.git.commits) == 0
        passed.append("novel: no signature, 0 auto-edit")

    # 7) --reason OFF: legacy behaviour unchanged (no reasoning note, no LLM
    #    briefing) but macro-fit still auto-heals.
    with tempfile.TemporaryDirectory() as tmp:
        ag = build_agent(tmp, reason=False)
        res = drive(ag, "security", LOGS["macro_fit"], pass_after=2)
        assert res.status == "DONE" and len(ag.llm.calls) == 0
        with tempfile.TemporaryDirectory() as tmp2:
            ag2 = build_agent(tmp2, reason=False)
            r2 = drive(ag2, "user_domain", LOGS["timing"])
            assert ag2.llm.calls and ag2.llm.calls[0]["reasoning"] == "", \
                "reason OFF => empty briefing"
        passed.append("reason-off: legacy behaviour preserved")

    print("PASS:")
    for p in passed:
        print("  -", p)
    print(f"\nALL {len(passed)} SCENARIOS PASSED")


if __name__ == "__main__":
    main()
