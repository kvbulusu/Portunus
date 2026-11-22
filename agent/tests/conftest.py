"""Portunus test configuration.

Many tests exercise the agent against the JANUS reference design (its ORFS
configs, SDC, Tcl hooks and RTL). Those files are not part of Portunus, so the
tests listed in reference_design_tests.txt are skipped unless a reference
design tree is present next to agent/ (i.e. a ``configs/`` directory exists at
the flow root, as in janus-ai-soc/asic_flow). Set PORTUNUS_RUN_REFERENCE_TESTS=1
to force them.
"""
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_FLOW_ROOT = os.path.dirname(os.path.dirname(_HERE))

with open(os.path.join(_HERE, "reference_design_tests.txt"), encoding="utf-8") as fh:
    _REFERENCE = {ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")}

if not (os.path.isdir(os.path.join(_FLOW_ROOT, "configs"))
        or os.environ.get("PORTUNUS_RUN_REFERENCE_TESTS") == "1"):
    collect_ignore = sorted(_REFERENCE)
