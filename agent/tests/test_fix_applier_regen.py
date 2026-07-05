#!/usr/bin/env python3
"""
test_fix_applier_regen.py — proves the SRAM-reshape SAFETY properties that the
real 'security' run exposed:

  A) FakeRAM only builds banks in {1,2,4}; an illegal banks value is REFUSED
     before any edit (no crash, no commit).
  B) column_mux_factor outside {1,2,4,8} is refused.
  C) When regen is ENABLED and FAILS, the script edit is ROLLED BACK and the
     result is ok=False (so the agent never commits an unbuildable macro —
     the bug that stranded commit f02537d).
  D) When regen SUCCEEDS, the edit sticks and ok=True (repo_file set to commit).
  E) With --no-sram-regen (regen disabled), the edit sticks, ok=True, and the
     detail says regen was skipped.
"""
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
from fix_applier import FixApplier  # noqa: E402

SCRIPT_TMPL = """#!/usr/bin/env bash
# minimal stand-in for scripts/04_setup_srams.sh — carries the SRAMS array and
# the column_mux_factor the applier edits, then exits with a controllable code.
SRAMS=(
  "fakeram7_2048x128  128  2048  1"
  "fakeram7_256x128   128   256  1"
)
# python heredoc marker the applier's regex looks for:
: <<'PY'
cfg["column_mux_factor"] = 4
PY
exit {rc}
"""


def make_script(tmp, rc):
    p = os.path.join(tmp, "04_setup_srams.sh")
    with open(p, "w") as fh:
        fh.write(SCRIPT_TMPL.format(rc=rc))
    return p


def banks_of(path, macro):
    import re
    txt = open(path).read()
    m = re.search(rf'"{macro}\s+\d+\s+\d+\s+(\d+)', txt)
    return int(m.group(1)) if m else None


def colmux_of(path):
    import re
    m = re.search(r'column_mux_factor"\]\s*=\s*(\d+)', open(path).read())
    return int(m.group(1)) if m else None


def main():
    passed = []

    # A) illegal banks refused, script untouched
    with tempfile.TemporaryDirectory() as tmp:
        s = make_script(tmp, rc=0)
        fa = FixApplier(block_config_path=os.path.join(tmp, "config.mk"), block_sdc_path=os.path.join(tmp, "constraint.sdc"), rtl_root=tmp, repo_config_path=None, sram_setup_script=s, regen_srams=True)
        r = fa.apply([{"type": "sram_reshape", "script": s,
                       "macro": "fakeram7_2048x128", "banks": 64,
                       "column_mux_factor": 4}])[0]
        assert not r.ok and "illegal" in r.detail.lower(), r.detail
        assert banks_of(s, "fakeram7_2048x128") == 1, "script must be untouched"
        passed.append("A: illegal banks=64 refused, script untouched")

    # B) illegal col_mux refused
    with tempfile.TemporaryDirectory() as tmp:
        s = make_script(tmp, rc=0)
        fa = FixApplier(block_config_path=os.path.join(tmp, "config.mk"), block_sdc_path=os.path.join(tmp, "constraint.sdc"), rtl_root=tmp, repo_config_path=None, sram_setup_script=s, regen_srams=True)
        r = fa.apply([{"type": "sram_reshape", "script": s,
                       "macro": "fakeram7_2048x128", "banks": 2,
                       "column_mux_factor": 3}])[0]
        assert not r.ok and "column_mux" in r.detail.lower(), r.detail
        passed.append("B: illegal col_mux=3 refused")

    # C) regen ENABLED + FAILS -> rollback + ok=False (the safety property)
    with tempfile.TemporaryDirectory() as tmp:
        s = make_script(tmp, rc=1)   # regen (bash script) returns non-zero
        fa = FixApplier(block_config_path=os.path.join(tmp, "config.mk"), block_sdc_path=os.path.join(tmp, "constraint.sdc"), rtl_root=tmp, repo_config_path=None, sram_setup_script=s, regen_srams=True)
        r = fa.apply([{"type": "sram_reshape", "script": s,
                       "macro": "fakeram7_2048x128", "banks": 2,
                       "column_mux_factor": 4}])[0]
        assert not r.ok, ("regen failure MUST yield ok=False", r.detail)
        assert "regen FAILED" in r.detail, r.detail
        assert banks_of(s, "fakeram7_2048x128") == 1, \
            ("script MUST be rolled back to banks=1", banks_of(s, "fakeram7_2048x128"))
        assert not fa.applied_any([r]), "applied_any must be False -> no commit"
        passed.append("C: regen-fail rolled back edit, ok=False, no commit")

    # D) regen SUCCEEDS -> edit sticks, ok=True, repo_file set
    with tempfile.TemporaryDirectory() as tmp:
        s = make_script(tmp, rc=0)
        fa = FixApplier(block_config_path=os.path.join(tmp, "config.mk"), block_sdc_path=os.path.join(tmp, "constraint.sdc"), rtl_root=tmp, repo_config_path=None, sram_setup_script=s, regen_srams=True)
        r = fa.apply([{"type": "sram_reshape", "script": s,
                       "macro": "fakeram7_2048x128", "banks": 2,
                       "column_mux_factor": 4}])[0]
        assert r.ok and r.repo_file == s, r.detail
        assert banks_of(s, "fakeram7_2048x128") == 2, "edit must stick"
        passed.append("D: regen-ok edit sticks, ok=True, repo_file set")

    # E) regen DISABLED -> edit sticks, ok=True, detail says skipped
    with tempfile.TemporaryDirectory() as tmp:
        s = make_script(tmp, rc=1)   # would fail, but regen is disabled
        fa = FixApplier(block_config_path=os.path.join(tmp, "config.mk"), block_sdc_path=os.path.join(tmp, "constraint.sdc"), rtl_root=tmp, repo_config_path=None, sram_setup_script=s, regen_srams=False)
        r = fa.apply([{"type": "sram_reshape", "script": s,
                       "macro": "fakeram7_256x128", "banks": 4,
                       "column_mux_factor": 4}])[0]
        assert r.ok and "skipped" in r.detail, r.detail
        assert banks_of(s, "fakeram7_256x128") == 4, "edit must stick"
        passed.append("E: --no-sram-regen edit sticks, ok=True, regen skipped")

    print("PASS:")
    for p in passed:
        print("  -", p)
    print(f"\nALL {len(passed)} FIX-APPLIER SAFETY CHECKS PASSED")


if __name__ == "__main__":
    main()
