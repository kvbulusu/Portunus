"""
fix_applier.py — Apply LLM-suggested fixes to ORFS config / SDC / RTL, safely.

Supported fix types (from llm_interface):
  * config      : set an ORFS make variable in the block's config.mk
  * sdc_period  : change a clock period in the block's constraint.sdc
  * rtl         : exact find/replace in an RTL file (guarded; minimal edits only)
  * manual      : not applied — returned for the human to handle

Safety model:
  * Every edited file is backed up to <file>.janusbak.<n> before the first edit
    in a run, so changes can be rolled back if the retry does not improve things.
  * RTL edits require an EXACT unique match of `find`; ambiguous or missing
    matches are refused (we never guess).
  * Config/SDC edits validate the new value looks sane (numeric where expected).
  * Nothing outside the block's own config dir / the repo RTL tree is touched.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class AppliedFix:
    type: str
    ok: bool
    detail: str
    file: Optional[str] = None
    # If the same edit was also mirrored into the repository *source* (so it
    # survives a re-stamp by scripts/03 and can be git-committed), the repo path
    # is recorded here. The orchestrator collects these to hand to GitOps.
    repo_file: Optional[str] = None


@dataclass
class FixApplier:
    block_config_path: str          # .../janus/<block>/config.mk  (STAMPED copy in ORFS tree)
    block_sdc_path: str             # .../janus/<block>/constraint.sdc (STAMPED copy)
    rtl_root: str                   # repo root (rtl/ lives under here)
    logger: Any = None
    # Repository SOURCE files that the stamped copies were generated from. When
    # provided, config/sdc fixes are mirrored here too, so they persist across
    # `scripts/03_setup_janus_configs.sh` re-stamping and can be committed.
    #   repo_config_path -> asic_flow/configs/<block>.mk
    #   repo_sdc_path     -> asic_flow/configs/constraints/<block>.sdc
    repo_config_path: Optional[str] = None
    repo_sdc_path: Optional[str] = None
    _backed_up: set = field(default_factory=set)

    def __post_init__(self):
        self.log = self.logger or (lambda *a, **k: None)

    # ---- backup / rollback --------------------------------------------------
    def _backup(self, path: str) -> None:
        if path in self._backed_up or not os.path.isfile(path):
            return
        bak = f"{path}.janusbak"
        if not os.path.exists(bak):
            shutil.copy2(path, bak)
        self._backed_up.add(path)

    def rollback_all(self) -> List[str]:
        restored = []
        for path in list(self._backed_up):
            bak = f"{path}.janusbak"
            if os.path.exists(bak):
                shutil.copy2(bak, path)
                restored.append(path)
        return restored

    # ---- dispatch -----------------------------------------------------------
    def apply(self, fixes: List[Dict]) -> List[AppliedFix]:
        results: List[AppliedFix] = []
        for fx in fixes:
            t = (fx.get("type") or "").lower()
            try:
                if t == "config":
                    results.append(self._apply_config(fx))
                elif t == "sdc_period":
                    results.append(self._apply_sdc_period(fx))
                elif t == "rtl":
                    results.append(self._apply_rtl(fx))
                elif t == "manual":
                    results.append(AppliedFix("manual", False,
                                              fx.get("instruction", "(no instruction)")))
                else:
                    results.append(AppliedFix(t or "unknown", False, f"unsupported fix type: {t}"))
            except Exception as e:
                results.append(AppliedFix(t, False, f"exception while applying: {e}"))
        return results

    def applied_any(self, results: List[AppliedFix]) -> bool:
        return any(r.ok for r in results)

    # ---- config.mk ----------------------------------------------------------
    _ALLOWED_CONFIG_KEYS = {
        "CORE_UTILIZATION", "PLACE_DENSITY", "CORE_ASPECT_RATIO", "CORE_MARGIN",
        "SYNTH_HIERARCHICAL", "ABC_AREA", "ABC_CLOCK_PERIOD_IN_PS",
    }

    def _apply_config(self, fx: Dict) -> AppliedFix:
        key = (fx.get("key") or "").strip()
        val = str(fx.get("value", "")).strip()
        if key not in self._ALLOWED_CONFIG_KEYS:
            return AppliedFix("config", False, f"refused: key '{key}' not in allow-list")
        if not _looks_sane(key, val):
            return AppliedFix("config", False, f"refused: value '{val}' for '{key}' looks invalid")
        path = self.block_config_path
        if not os.path.isfile(path):
            return AppliedFix("config", False, f"config not found: {path}", path)

        # Replace an existing 'export KEY = ...' or append a new one.
        pat = re.compile(rf"^(\s*export\s+{re.escape(key)}\s*=).*$", re.M)

        def _transform(text: str) -> str:
            if pat.search(text):
                return pat.sub(rf"\1 {val}", text, count=1)
            return text.rstrip() + f"\nexport {key} = {val}   # added by JANUS agent\n"

        self._backup(path)
        self._rewrite(path, _transform)
        # Mirror into the repo source so the fix survives a scripts/03 re-stamp
        # and can be committed. Config filename == block name (e.g. security.mk).
        repo_file = self._mirror(self.repo_config_path, _transform)
        detail = f"set {key}={val}"
        if repo_file:
            detail += " (mirrored to repo source)"
        return AppliedFix("config", True, detail, path, repo_file=repo_file)

    # ---- constraint.sdc -----------------------------------------------------
    def _apply_sdc_period(self, fx: Dict) -> AppliedFix:
        clock = (fx.get("clock") or "").strip()
        val = str(fx.get("value", "")).strip()
        if not _is_pos_float(val):
            return AppliedFix("sdc_period", False, f"refused: period '{val}' not a positive number")
        path = self.block_sdc_path
        if not os.path.isfile(path):
            return AppliedFix("sdc_period", False, f"sdc not found: {path}", path)
        with open(path) as fh:
            probe = fh.read()

        # Decide WHICH substitution applies (based on the stamped file), then
        # apply that exact same transform to both the stamped and repo copies.
        transform = None
        # Case A: variable-driven SDC (set clk_period 1.25)
        if re.search(r"^\s*set\s+clk_period\b", probe, re.M) and (clock in ("", "core_clk")
                or re.search(r"set\s+clk_name\s+" + re.escape(clock), probe)):
            transform = lambda t: re.sub(r"^(\s*set\s+clk_period\s+).*$",
                                         rf"\g<1>{val}", t, count=1, flags=re.M)
        # Case B: explicit create_clock -name <clock> -period <p>
        if transform is None and clock:
            pat = re.compile(rf"(create_clock\s+-name\s+{re.escape(clock)}\s+-period\s+)[\d.]+")
            if pat.search(probe):
                transform = lambda t, _p=pat: _p.sub(rf"\g<1>{val}", t, count=1)
        # Case C: single unnamed create_clock -period
        if transform is None:
            pat = re.compile(r"(create_clock\s+(?:-name\s+\S+\s+)?-period\s+)[\d.]+")
            if pat.search(probe):
                transform = lambda t, _p=pat: _p.sub(rf"\g<1>{val}", t, count=1)

        if transform is None:
            return AppliedFix("sdc_period", False, f"could not locate clock '{clock}' period in SDC", path)

        self._backup(path)
        self._rewrite(path, transform)
        repo_file = self._mirror(self.repo_sdc_path, transform)
        detail = f"set period of '{clock or 'clock'}' to {val} ns"
        if repo_file:
            detail += " (mirrored to repo source)"
        return AppliedFix("sdc_period", True, detail, path, repo_file=repo_file)

    # ---- shared write / mirror helpers --------------------------------------
    def _rewrite(self, path: str, transform) -> None:
        """Apply `transform(text)->text` to a file in place."""
        with open(path) as fh:
            text = fh.read()
        with open(path, "w") as fh:
            fh.write(transform(text))

    def _mirror(self, repo_path: Optional[str], transform) -> Optional[str]:
        """Apply the same transform to the repo SOURCE file, if provided and it
        exists. Returns the repo path on success (for git), else None. Never
        raises — a mirror failure must not break the primary (stamped) edit."""
        if not repo_path or not os.path.isfile(repo_path):
            return None
        try:
            self._backup(repo_path)
            self._rewrite(repo_path, transform)
            return repo_path
        except Exception as e:  # pragma: no cover
            self.log(f"[fix] could not mirror to repo source {repo_path}: {e}")
            return None

    # ---- RTL edit -----------------------------------------------------------
    def _apply_rtl(self, fx: Dict) -> AppliedFix:
        rel = (fx.get("file") or "").strip()
        find = fx.get("find")
        replace = fx.get("replace")
        if not rel or find is None or replace is None:
            return AppliedFix("rtl", False, "refused: rtl fix needs file/find/replace")
        # Resolve and confine to the repo rtl/ tree.
        path = rel if os.path.isabs(rel) else os.path.join(self.rtl_root, rel)
        path = os.path.realpath(path)
        rtl_dir = os.path.realpath(os.path.join(self.rtl_root, "rtl"))
        if not path.startswith(rtl_dir):
            return AppliedFix("rtl", False, f"refused: '{rel}' is outside the rtl/ tree", path)
        if not os.path.isfile(path):
            return AppliedFix("rtl", False, f"file not found: {path}", path)
        with open(path) as fh:
            text = fh.read()
        n = text.count(find)
        if n == 0:
            return AppliedFix("rtl", False, "refused: 'find' text not present (no guessing)", path)
        if n > 1:
            return AppliedFix("rtl", False, f"refused: 'find' matches {n} places (not unique)", path)
        self._backup(path)
        with open(path, "w") as fh:
            fh.write(text.replace(find, replace, 1))
        # RTL lives in the repo tree, so the edited path IS the commit target.
        return AppliedFix("rtl", True, f"applied 1 edit in {os.path.relpath(path, self.rtl_root)}",
                          path, repo_file=path)


# ---- validators -------------------------------------------------------------
def _is_pos_float(v: str) -> bool:
    try:
        return float(v) > 0
    except ValueError:
        return False


def _looks_sane(key: str, val: str) -> bool:
    if key in ("CORE_UTILIZATION",):
        return val.isdigit() and 1 <= int(val) <= 90
    if key in ("PLACE_DENSITY", "CORE_ASPECT_RATIO"):
        return _is_pos_float(val) and float(val) <= 5
    if key in ("CORE_MARGIN", "ABC_CLOCK_PERIOD_IN_PS"):
        return _is_pos_float(val)
    if key == "SYNTH_HIERARCHICAL":
        return val in ("0", "1")
    if key == "ABC_AREA":
        return val in ("0", "1")
    return True


# ---- self-test --------------------------------------------------------------
if __name__ == "__main__":
    import tempfile
    d = tempfile.mkdtemp()
    cfg = os.path.join(d, "config.mk")
    sdc = os.path.join(d, "constraint.sdc")
    open(cfg, "w").write("export CORE_UTILIZATION = 60\nexport DESIGN_NAME = x\n")
    open(sdc, "w").write("set clk_period 1.25\ncreate_clock -name core_clk -period 1.25 [get_ports clk]\n")
    fa = FixApplier(cfg, sdc, d)
    r1 = fa.apply([{"type": "config", "key": "CORE_UTILIZATION", "value": "50", "reason": "shrink"}])
    r2 = fa.apply([{"type": "sdc_period", "clock": "core_clk", "value": "1.50", "reason": "relax"}])
    r3 = fa.apply([{"type": "config", "key": "EVIL", "value": "9", "reason": "x"}])
    assert r1[0].ok and "50" in open(cfg).read()
    assert r2[0].ok and "1.50" in open(sdc).read()
    assert not r3[0].ok  # allow-list rejection
    restored = fa.rollback_all()
    assert "60" in open(cfg).read(), "rollback failed"
    print("fix_applier self-test OK; rolled back:", [os.path.basename(p) for p in restored])
