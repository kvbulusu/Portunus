"""§173 user_domain: probe D sweep (refpin@D), two-corner D, per-kind margin, corner-label fix."""
import os
import shutil
import subprocess

import pytest

ASIC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPTS = os.path.join(ASIC, "scripts")
SH = os.path.join(SCRIPTS, "ud_cts_probe.sh")
TCL = os.path.join(SCRIPTS, "ud_cts_probe.tcl")


def _read(p):
    with open(p) as f:
        return f.read()


def test_corner_label_uses_get_name():
    t = _read(TCL)
    assert "[$c name]" not in t  # scene objects have no 'name' method
    assert "get_name $c" in t


def test_tcl_reports_dfast_margin_sweeprow():
    t = _read(TCL)
    for frag in ('P "D: slow', "arrival_min_rise", "PROBE_MARGIN_WIN", "-slack_max $win",
                 "SWEEPROW tgt", "set tgt 0\nif { $mode ne \"ideal\" }"):
        assert frag in t, frag
    assert "write_db" not in t


@pytest.mark.skipif(shutil.which("tclsh") is None, reason="tclsh not installed")
def test_tcl_parses():
    out = subprocess.run(["tclsh"], input=f"puts [info complete [read [open {{{TCL}}}]]]\n",
                         capture_output=True, text=True, timeout=30)
    assert out.stdout.strip() == "1", out.stderr


def _fake_flow(tmp_path):
    flow = tmp_path / "flow"
    (flow / "designs/asap7/janus/user_domain").mkdir(parents=True)
    (flow / "designs/asap7/janus/user_domain/config.mk").write_text("")
    (flow / "results/asap7/user_domain/base").mkdir(parents=True)
    (flow / "results/asap7/user_domain/base/3_place.odb").write_text("")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    make = bindir / "make"
    make.write_text('#!/bin/bash\nprintf "hook: x\\nSWEEPROW tgt %s mode %s\\n" "$PROBE_FIX171_TARGET" '
                    '"$PROBE_MODE" > "$PROBE_OUT"\necho "$PROBE_MODE $PROBE_FIX171_TARGET" >> "$CALLS"\n')
    make.chmod(0o755)
    return flow, bindir


def test_sh_sweep_expands_and_tabulates(tmp_path):
    flow, bindir = _fake_flow(tmp_path)
    calls = tmp_path / "calls"
    env = dict(os.environ, FLOW_HOME=str(flow), PATH=f"{bindir}:{os.environ['PATH']}",
               CALLS=str(calls), PROBE_SWEEP="1166 1500", PROBE_JOBS="1")
    out = subprocess.run(["bash", SH, "user_domain", "sweep fix171 refpin@1700"],
                         capture_output=True, text=True, env=env, timeout=60)
    assert out.returncode == 0, out.stderr
    got = sorted(x.strip() for x in calls.read_text().split("\n")[:-1])
    assert got == sorted(["refpin 1166", "refpin 1500", "fix171", "refpin 1700"]), got
    s = out.stdout
    assert "D SWEEP" in s
    for row in ("refpin@1166    tgt 1166 mode refpin", "refpin@1500    tgt 1500",
                "refpin@1700    tgt 1700", "fix171         tgt  mode fix171"):
        assert row in s, row
    # runs/ dir and tarball land under the repo; clean up what this test made
    for line in s.splitlines():
        if line.startswith("Reports: "):
            d = line.split()[1]
            shutil.rmtree(d, ignore_errors=True)
            os.remove(d + ".tar.gz")
