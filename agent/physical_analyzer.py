"""
physical_analyzer.py — Deterministic floorplan / macro-placement analysis for
the JANUS ASIC agent.

WHY THIS EXISTS
---------------
Some ORFS failures are neither environment bugs (handled by log_analyzer's
triage) nor small config/SDC/RTL edits the LLM can make. The canonical example
is a macro that will not fit in the core:

    [ERROR MPL-0006] Found macro <inst> ... that does not fit in the core

For JANUS this is almost always a *pathological SRAM aspect ratio*: FakeRAM2.0
lays a single-port macro out as (depth / F) bitcell rows by (width_bits * F)
columns, where F = banks * column_mux_factor. With the tool defaults
(column_mux_factor = 1, banks = 1) the deep memories come out as extreme
slivers, e.g.

    fakeram7_8192x32  ->   4.18 x 2654.4 um   (635:1 !!)
    fakeram7_2048x128 ->  16.72 x  663.6 um   (40:1)

which RTL-MP cannot place. The fix is not a config knob on the *block* — it is
to RESHAPE the macro by folding depth into more columns (raise banks /
column_mux_factor), which leaves the bitcell count and the pin interface
identical but makes the macro roughly square.

This module reproduces — deterministically, with no LLM call — the exact
analysis a human did by hand:
  1. detect the MPL-0006 macro-fit failure in a log,
  2. read the current SRAM geometry (from generated LEFs and/or the model),
  3. compute, per macro, the `banks` fold that gives the most square shape
     that fits the core, and
  4. emit `sram_reshape` fix descriptors the FixApplier can apply to
     scripts/04_setup_srams.sh (the tracked source of truth) + regenerate.

Pure stdlib. No network. Safe to import anywhere.
"""

from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


# --- detection ---------------------------------------------------------------
# RTL-MP "does not fit" error. Message wording varies slightly across OpenROAD
# versions, so match the stable code plus a couple of phrasings.
_MPL0006_RE = re.compile(r"\bMPL-0006\b", re.I)
_MACRO_NOFIT_RE = re.compile(r"macro.*(does not fit|cannot fit|larger than).*core", re.I)
# Pull a fakeram-style master or an instance path out of the error text.
_INST_RE = re.compile(r"([A-Za-z_][\w./\\\[\]$-]*u_ram\b)", re.I)
_FAKERAM_MASTER_RE = re.compile(r"(fakeram\w*)", re.I)
# "fakeram7_2048x128" -> depth=2048, width_bits=128  (name is <tech>_<depth>x<width>)
_FAKERAM_GEOM_RE = re.compile(r"fakeram\d*_(\d+)x(\d+)", re.I)
# core / macro dimension hints occasionally present in the log
_CORE_AREA_RE = re.compile(r"core.*?(\d+\.?\d*)\s*(?:um|x|,)\s*(\d+\.?\d*)", re.I)


@dataclass
class MacroFitIssue:
    """A detected 'macro does not fit in core' failure."""
    matched_line: str
    instance: Optional[str] = None          # e.g. u_kv_cache/.../u_ram
    master_hint: Optional[str] = None        # e.g. fakeram7_2048x128 (if in log)
    core_w: Optional[float] = None
    core_h: Optional[float] = None

    def to_dict(self) -> Dict:
        return {
            "matched_line": self.matched_line, "instance": self.instance,
            "master_hint": self.master_hint,
            "core_w": self.core_w, "core_h": self.core_h,
        }


def detect_macro_fit_failure(error_lines: List[str], tail: str = "") -> Optional[MacroFitIssue]:
    """Return a MacroFitIssue if the log shows an RTL-MP macro-fit failure.

    Scans the captured error lines first (most specific), then the log tail.
    Returns None if this is not a macro-fit failure (caller falls through to
    the normal LLM / triage path).
    """
    hay: List[str] = list(error_lines)
    if tail:
        hay.extend(tail.splitlines())
    for line in hay:
        if _MPL0006_RE.search(line) or _MACRO_NOFIT_RE.search(line):
            issue = MacroFitIssue(matched_line=line.strip()[:240])
            mi = _INST_RE.search(line)
            if mi:
                issue.instance = mi.group(1)
            mm = _FAKERAM_MASTER_RE.search(line)
            if mm:
                issue.master_hint = mm.group(1)
            return issue
    return None


# --- geometry model ----------------------------------------------------------
# FakeRAM2.0's abstract layout scales as:
#     height  = KH * depth      / F
#     width   = KW * width_bits  * F          (F = banks * column_mux_factor)
# The constants are tech dependent. We DON'T hard-trust them: when a current
# LEF is available we derive the block's actual shape and scale from it (exact).
# These ASAP7 fallbacks (empirically 0.328 / 0.130) are only used when no LEF
# has been generated yet.
_KH_ASAP7 = 0.328
_KW_ASAP7 = 0.130
_BANK_CANDIDATES = (1, 2, 4, 8, 16, 32, 64)


@dataclass
class MacroGeom:
    name: str
    depth: int
    width_bits: int
    banks: int                      # current banks in the setup script
    col_mux: int                    # current global column_mux_factor
    lef_w: Optional[float] = None   # measured from generated LEF, if present
    lef_h: Optional[float] = None

    @property
    def cur_F(self) -> int:
        return max(1, self.banks * self.col_mux)

    def predict(self, banks: int, col_mux: Optional[int] = None) -> Tuple[float, float]:
        """Predict (width_um, height_um) for a candidate banks/col_mux.

        Uses exact ratio-scaling from the measured LEF when available (so it is
        independent of the tech constants); otherwise the ASAP7 model.
        """
        cm = col_mux if col_mux is not None else self.col_mux
        F = max(1, banks * cm)
        if self.lef_w and self.lef_h:
            # width ∝ F, height ∝ 1/F  -> scale from the known (cur_F) point.
            w = self.lef_w * (F / self.cur_F)
            h = self.lef_h * (self.cur_F / F)
            return (round(w, 2), round(h, 2))
        w = _KW_ASAP7 * self.width_bits * F
        h = _KH_ASAP7 * self.depth / F
        return (round(w, 2), round(h, 2))

    @staticmethod
    def aspect(w: float, h: float) -> float:
        lo, hi = min(w, h), max(w, h)
        return (hi / lo) if lo > 0 else float("inf")


@dataclass
class ReshapeProposal:
    macro: str
    old_banks: int
    new_banks: int
    col_mux: int
    old_dims: Tuple[float, float]
    new_dims: Tuple[float, float]
    reason: str

    def to_fix(self, script_path: str) -> Dict:
        """Serialize as an `sram_reshape` fix descriptor for the FixApplier."""
        return {
            "type": "sram_reshape",
            "script": script_path,
            "macro": self.macro,
            "banks": self.new_banks,
            "column_mux_factor": self.col_mux,
            "reason": self.reason,
        }

    def to_dict(self) -> Dict:
        d = dict(self.__dict__)
        return d


def recommend_banks(geom: MacroGeom,
                    col_mux: Optional[int] = None,
                    core_w: Optional[float] = None,
                    core_h: Optional[float] = None,
                    target_aspect: float = 3.2) -> Optional[ReshapeProposal]:
    """Choose the `banks` value giving the most square macro that fits the core.

    Selection: among the candidate bank folds, prefer those that FIT the core
    (both dims <= core dims, if core dims known), then minimize aspect ratio.
    Returns None if the current shape is already good (no reshape needed).
    """
    cm = col_mux if col_mux is not None else geom.col_mux
    old_dims = geom.predict(geom.banks, cm)
    old_aspect = MacroGeom.aspect(*old_dims)

    def fits(w: float, h: float) -> bool:
        if core_w is None or core_h is None:
            return True
        # allow a small margin; RTL-MP needs the macro strictly inside the core
        return w <= core_w * 0.98 and h <= core_h * 0.98

    best: Optional[Tuple] = None  # (fits_flag, aspect, banks, dims)
    for banks in _BANK_CANDIDATES:
        w, h = geom.predict(banks, cm)
        asp = MacroGeom.aspect(w, h)
        key = (0 if fits(w, h) else 1, asp)
        if best is None or key < best[0]:
            best = (key, banks, (w, h), asp)
    if best is None:
        return None
    _, new_banks, new_dims, new_aspect = best

    # No change needed if we're already square enough AND (fits or unknown core).
    already_ok = old_aspect <= target_aspect and (
        core_w is None or (old_dims[0] <= core_w and old_dims[1] <= core_h))
    if new_banks == geom.banks and already_ok:
        return None
    # Don't propose a change that isn't actually better.
    if new_aspect >= old_aspect and new_banks == geom.banks:
        return None

    reason = (f"reshape {geom.name}: banks {geom.banks}->{new_banks} "
              f"(col_mux={cm}) so macro goes {old_dims[0]}x{old_dims[1]}um "
              f"(aspect {old_aspect:.1f}:1) -> {new_dims[0]}x{new_dims[1]}um "
              f"(aspect {new_aspect:.1f}:1); bitcell count & pins unchanged.")
    return ReshapeProposal(
        macro=geom.name, old_banks=geom.banks, new_banks=new_banks,
        col_mux=cm, old_dims=old_dims, new_dims=new_dims, reason=reason)


# --- reading the current setup (script + LEFs) -------------------------------
_SRAMS_ARRAY_RE = re.compile(r"SRAMS=\((.*?)\n\)", re.S)
_COLMUX_RE = re.compile(r'cfg\["column_mux_factor"\]\s*=\s*(\d+)')
_LEF_SIZE_RE = re.compile(r"SIZE\s+([\d.]+)\s+BY\s+([\d.]+)", re.I)


def parse_setup_script(script_path: str) -> Tuple[List[MacroGeom], int]:
    """Parse scripts/04_setup_srams.sh -> ([MacroGeom...], column_mux_factor).

    Reads the SRAMS=( ... ) array (each row: 'name width depth banks') and the
    global column_mux_factor set in the python heredoc. Returns an empty list
    if the script can't be parsed (caller then skips deterministic reshaping).
    """
    try:
        with open(script_path) as fh:
            text = fh.read()
    except OSError:
        return [], 1
    col_mux = 1
    m = _COLMUX_RE.search(text)
    if m:
        col_mux = int(m.group(1))
    geoms: List[MacroGeom] = []
    am = _SRAMS_ARRAY_RE.search(text)
    if am:
        for raw in am.group(1).splitlines():
            line = raw.split("#", 1)[0].strip().strip('"')
            if not line:
                continue
            parts = line.split()
            if len(parts) < 3:
                continue
            name = parts[0]
            try:
                width_bits = int(parts[1])
                depth = int(parts[2])
                banks = int(parts[3]) if len(parts) > 3 else 1
            except ValueError:
                continue
            geoms.append(MacroGeom(name=name, depth=depth, width_bits=width_bits,
                                   banks=banks, col_mux=col_mux))
    return geoms, col_mux


def measure_lef(sram_dir: str, name: str) -> Tuple[Optional[float], Optional[float]]:
    """Return (width_um, height_um) from <sram_dir>/<name>.lef, or (None, None)."""
    path = os.path.join(sram_dir, f"{name}.lef")
    if not os.path.isfile(path):
        # fall back to any matching LEF under the dir
        hits = glob.glob(os.path.join(sram_dir, f"{name}.lef"))
        path = hits[0] if hits else path
    try:
        with open(path) as fh:
            for line in fh:
                m = _LEF_SIZE_RE.search(line)
                if m:
                    return (float(m.group(1)), float(m.group(2)))
    except OSError:
        pass
    return (None, None)


def analyze_and_recommend(script_path: str,
                          sram_dir: Optional[str] = None,
                          issue: Optional[MacroFitIssue] = None,
                          target_aspect: float = 3.2) -> List[ReshapeProposal]:
    """Full deterministic analysis: read the SRAM setup, measure current LEFs,
    and return reshape proposals for every macro whose shape is pathological
    (or that the specific failing `issue` points at).

    This is the automated version of the hand analysis: it needs NO LLM call.
    """
    geoms, col_mux = parse_setup_script(script_path)
    if not geoms:
        return []
    core_w = issue.core_w if issue else None
    core_h = issue.core_h if issue else None
    proposals: List[ReshapeProposal] = []
    for g in geoms:
        if sram_dir:
            g.lef_w, g.lef_h = measure_lef(sram_dir, g.name)
        prop = recommend_banks(g, col_mux=col_mux, core_w=core_w,
                               core_h=core_h, target_aspect=target_aspect)
        if prop is not None:
            proposals.append(prop)
    return proposals


# --- self-test ---------------------------------------------------------------
if __name__ == "__main__":
    # 1) detection
    log = ("[INFO] placing macros\n"
           "[ERROR MPL-0006] Found macro u_kv_cache/u_spm/g_bank[0]."
           "g_deep_macro.u_macro_a/u_ram that does not fit in the core\n")
    issue = detect_macro_fit_failure(log.splitlines())
    assert issue and "u_ram" in (issue.instance or ""), issue
    print("detect OK:", issue.instance)

    # 2) geometry model reproduces the known pathological -> reshaped result
    #    8192x32 with banks=1,colmux=1 is a 635:1 sliver; recommender should
    #    fold it to a near-square shape.
    g = MacroGeom(name="fakeram7_8192x32", depth=8192, width_bits=32,
                  banks=1, col_mux=1)
    w, h = g.predict(1, 1)
    assert MacroGeom.aspect(w, h) > 100, (w, h)
    prop = recommend_banks(g, col_mux=4)  # emulate raising global col_mux to 4
    assert prop and prop.new_banks >= 2, prop
    a_new = MacroGeom.aspect(*prop.new_dims)
    assert a_new <= 5, prop
    print("reshape OK:", prop.reason)

    # 3) a healthy macro yields no proposal
    ok = MacroGeom(name="fakeram7_256x128", depth=256, width_bits=128,
                   banks=1, col_mux=4)
    assert recommend_banks(ok, col_mux=4) is None
    print("no-op OK for already-square macro")
    print("physical_analyzer self-test OK")
