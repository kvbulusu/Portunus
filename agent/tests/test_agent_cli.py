"""§126 CLI consolidation tests: --skip tokens, hidden legacy aliases,
adaptive flags and _validate_args() mutual exclusions.
Run: python3 -m pytest asic_flow/agent/tests/test_agent_cli.py -q"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import janus_agent as ja  # noqa: E402


def _parse(*argv):
    a = ja.build_parser().parse_args(list(argv))
    ja._apply_skips(a)
    return a


def _errs(*argv):
    return ja._validate_args(_parse(*argv))[0]


def test_defaults_clean():
    a = _parse()
    assert a.adaptive == "advise" and a.skip is None and not a.debug_on_fail
    assert ja._validate_args(a) == ([], [])


@pytest.mark.parametrize("tok", list(ja._SKIP_MAP))
def test_every_skip_token_sets_its_attr(tok):
    attr = ja._SKIP_MAP[tok][0]
    assert getattr(_parse(), attr) is False
    assert getattr(_parse("--skip", tok), attr) is True


@pytest.mark.parametrize("tok", list(ja._SKIP_MAP))
def test_legacy_alias_still_parses(tok):
    attr = ja._SKIP_MAP[tok][0]
    assert getattr(_parse(ja._flag(attr)), attr) is True


def test_skip_repeatable_extends():
    a = _parse("--skip", "sram", "--skip", "golden", "abstract")
    assert a.skip == ["sram", "golden", "abstract"]
    assert a.no_sram_regen and a.no_golden and a.no_abstract


def test_legacy_hidden_new_visible_in_help():
    h = ja.build_parser().format_help()
    for tok, (attr, _) in ja._SKIP_MAP.items():
        assert f"  {tok}" in h               # token listed in the --skip table
        assert f"  {ja._flag(attr)} " not in h  # legacy flag not an option row
    for f in ("--adaptive {off,advise,auto}", "--adaptive-max-passes",
              "--adaptive-thresholds", "--debug-on-fail"):
        assert f in h
    assert ".webp" not in h


def test_skip_bad_token_rejected():
    with pytest.raises(SystemExit):
        ja.build_parser().parse_args(["--skip", "bogus"])


@pytest.mark.parametrize("argv", [
    ("--status", "--gc"), ("--gc", "--gc-dry-run"), ("--estimate", "--signoff-only"),
    ("--triage-slew", "--resume-preview"), ("--signoff-only", "--post-triage-slew"),
    ("--resume-from", "3_place", "--clean"),
    ("--resume-from", "4_cts", "--force-blocks", "noc"),
    ("--git-push", "--skip", "git-commit"), ("--git-sync-branches", "main", "--no-git-commit"),
    ("--golden-include-heavy", "--skip", "golden"),
    ("--signoff-only", "--skip", "signoff"),
    ("--variants", "a", "b", "--variant", "x"),
    ("--extra-make", "NOEQUALS"),
    ("--adaptive", "off", "--adaptive-max-passes", "1"),
    ("--adaptive", "off", "--adaptive-thresholds", "{}"),
    ("--adaptive", "auto", "--dry-run"),
    ("--adaptive", "auto", "--adaptive-max-passes", "9"),
    ("--adaptive-thresholds", "{\"nope\": 1}"),
    ("--adaptive-thresholds", "[1,2]"),
    ("--adaptive-thresholds", "not json"),
    ("--parallel", "0"), ("--max-retries", "0"), ("--dashboard-port", "70000"),
])
def test_conflicts_rejected(argv):
    assert _errs(*argv), argv


@pytest.mark.parametrize("argv", [
    ("--resume", "--force-blocks", "noc"), ("--resume-from", "3_place"),
    ("--adaptive", "auto", "--adaptive-max-passes", "2"),
    ("--adaptive-thresholds", "{\"skew_frac_wns\": 0.4}"),
    ("--skip", "signoff", "signoff-sta", "debug-images"),
    ("--status", "--blocks", "noc"), ("--gc", "--yes"), ("--debug-on-fail",),
    ("--dry-run",), ("--extra-make", "A=1", "B=0"),
])
def test_valid_combos_accepted(argv):
    assert _errs(*argv) == [], argv


def test_thresholds_file(tmp_path):
    f = tmp_path / "th.json"
    f.write_text('{"max_extra_passes": 2}')
    assert _errs("--adaptive", "auto", "--adaptive-thresholds", str(f)) == []


def test_warnings():
    w = ja._validate_args(_parse("--yes"))[1]
    assert any("--yes" in x for x in w)
    w = ja._validate_args(_parse("--interactive", "--skip", "llm"))[1]
    assert any("--interactive" in x for x in w)


def test_main_returns_2_on_conflict(capsys):
    assert ja.main(["--status", "--gc"]) == 2
    assert "mutually exclusive" in capsys.readouterr().err
