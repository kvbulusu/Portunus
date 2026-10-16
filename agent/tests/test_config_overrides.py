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
