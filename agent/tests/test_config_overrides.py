"""§141: guard-band overrides must take effect (a '?=' after a plain '=' is a no-op)."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "utils"))
import flow_state  # noqa: E402

CFG = os.path.join(ROOT, "configs")


def _v(block, key):
    return flow_state.parse_config_vars(os.path.join(CFG, f"{block}.mk")).get(key)


def test_cva6_hold_margin_is_25():
    assert _v("user_core_cva6", "HOLD_SLACK_MARGIN") == "25"


def test_ara_lec_off():
    assert _v("user_core_ara", "LEC_CHECK") == "0"



def _make_value(cfg, var, env_val=None, local_mk=None, cmdline=None, tmp=None):
    """Evaluate VAR the way GNU make sees it when cfg is the DESIGN_CONFIG."""
    import shutil
    import subprocess
    import pytest
    if not shutil.which("make"):
        pytest.skip("GNU make not available")
    d = tmp
    with open(cfg) as f:
        body = f.read()
    # keep only the LOCAL include + the var's own assignments (the full ara .mk
    # references ORFS paths that do not exist here)
    keep = [ln for ln in body.splitlines()
            if ln.startswith("-include") or ln.split("?=")[0].split("=")[0].split()[-1:] == [var]]
    (d / "user_core_ara.mk").write_text("\n".join(keep) + "\n")
    if local_mk is not None:
        (d / "user_core_ara_local.mk").write_text(local_mk)
    (d / "Makefile").write_text(
        "include user_core_ara.mk\nshow:\n\t@printf '%s' \"$$" + var + "\"\n")
    env = {k: v for k, v in os.environ.items() if k != var}
    if env_val is not None:
        env[var] = env_val
    cmd = ["make", "-s", "-C", str(d), "show"] + ([f"{var}={cmdline}"] if cmdline else [])
    return subprocess.run(cmd, env=env, capture_output=True, text=True, check=True).stdout


def test_ara_lec_off_beats_env_and_local_mk(tmp_path):
    """§144: an inherited LEC_CHECK=1 (shell env or _local.mk) must NOT re-enable LEC."""
    cfg = os.path.join(CFG, "user_core_ara.mk")
    assert _make_value(cfg, "LEC_CHECK", tmp=tmp_path) == "0"
    assert _make_value(cfg, "LEC_CHECK", env_val="1", tmp=tmp_path) == "0"
    assert _make_value(cfg, "LEC_CHECK", local_mk="export LEC_CHECK = 1\n",
                       tmp=tmp_path) == "0"
    # deliberate opt-in via --extra-make (make command line) still wins
    assert _make_value(cfg, "LEC_CHECK", cmdline="1", tmp=tmp_path) == "1"
