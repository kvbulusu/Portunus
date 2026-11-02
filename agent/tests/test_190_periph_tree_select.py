"""§190 janus_soc_top setup: periph priority selects must be log2-depth trees.

Floorplan-stage QoR (ideal wires, run 20261101_123501) already showed WNS
-2.71 ns on clk_user (2857 ps). Every top path was in one of two linear scans:
  * plic_target: 15-source priority scan (x12 targets) -> gateways / prdata
  * job_queue:   16-entry priority select + free-slot scan -> vld/count/arbiter
GPL made it -7.9 ns and resize -5.8 ns, so placement can't fix it. Both
scans became balanced argmax trees. Equivalence was proven with yosys miter/
equiv_induct (PR body). The test below re-proves plic_target whenever yosys
is available.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
PLIC = ROOT / "rtl/periph/plic_target.sv"
JOBQ = ROOT / "rtl/periph/job_queue.sv"


def _code(path):
    return "\n".join(l.split("//", 1)[0] for l in path.read_text().splitlines())


def test_plic_target_has_no_linear_scan_190():
    src = _code(PLIC)
    assert not re.search(r"\bfor\s*\(\s*i\s*=", src), "linear source scan is back"
    assert "always_comb" not in src
    assert re.search(r"assign\s+found\s*=\s*t_v\[0\]", src)
    assert re.search(r"assign\s+best_id\s*=\s*t_id\[0\]", src)
    # ties -> lowest id: right subtree only on STRICTLY greater prio
    assert re.search(r"t_p\[2\*gn\+2\]\s*>\s*t_p\[2\*gn\+1\]", src)
    assert "gl >= 1" in src, "source 0 is reserved and must never win"


def test_job_queue_has_no_linear_scan_190():
    src = _code(JOBQ)
    assert not re.search(r"\bfor\s*\(\s*[sf]\s*=", src), "linear select/free scan is back"
    assert re.search(r"assign\s+sel_valid\s*=\s*s_v\[0\]", src)
    assert re.search(r"assign\s+sel_idx\s*=\s*s_id\[0\]", src)
    assert re.search(r"assign\s+free_idx\s*=\s*f_v\[0\]\s*\?\s*f_id\[0\]", src)
    assert re.search(r"s_p\[2\*gn\+2\]\s*>\s*s_p\[2\*gn\+1\]", src)
    # first free = left subtree preferred
    assert re.search(r"f_id\[gn\]\s*=\s*f_v\[2\*gn\+1\]\s*\?\s*f_id\[2\*gn\+1\]", src)


# Pre-§190 linear scan, kept verbatim as the golden reference.
_PLIC_REF = r"""
module plic_ref #(parameter int NUM_SOURCES = 16, parameter int PRIO_W = 3,
                  parameter int SRC_ID_W = $clog2(NUM_SOURCES)) (
  input  logic [NUM_SOURCES-1:0]        ip_i, enable_i,
  input  logic [NUM_SOURCES*PRIO_W-1:0] prio_i,
  input  logic [PRIO_W-1:0]             threshold_i,
  output logic eip_o, output logic [SRC_ID_W-1:0] best_id_o);
  logic found; logic [SRC_ID_W-1:0] best_id; logic [PRIO_W-1:0] best_prio, src_prio;
  integer i;
  always_comb begin
    found = 1'b0; best_id = '0; best_prio = '0;
    for (i = 1; i < NUM_SOURCES; i = i + 1) begin
      src_prio = prio_i[i*PRIO_W +: PRIO_W];
      if (ip_i[i] && enable_i[i] && (src_prio > threshold_i))
        if (!found || (src_prio > best_prio)) begin
          found = 1'b1; best_id = SRC_ID_W'(i); best_prio = src_prio;
        end
    end
  end
  assign eip_o = found; assign best_id_o = best_id;
endmodule
"""


def _yosys():
    for c in (os.environ.get("YOSYS"), shutil.which("yosys"),
              str(Path.home() / "OpenROAD-flow-scripts/tools/install/yosys/bin/yosys")):
        if c and Path(c).is_file():
            return c
    return None


@pytest.mark.parametrize("n,pw", [(16, 3), (13, 3), (8, 2)])
def test_plic_target_tree_equivalent_to_scan_190(tmp_path, n, pw):
    y = _yosys()
    if not y:
        pytest.skip("yosys not available")
    ref = tmp_path / "ref.sv"
    ref.write_text(_PLIC_REF)
    ports = set(re.findall(r"\b(\w+_[io])\b", PLIC.read_text()))
    if not {"ip_i", "enable_i", "prio_i", "threshold_i", "eip_o", "best_id_o"} <= ports:
        pytest.skip("plic_target port list changed; update reference")
    script = (f"read_verilog -sv {ref} {PLIC}; "
              f"chparam -set NUM_SOURCES {n} -set PRIO_W {pw} plic_ref plic_target; "
              "proc; opt_clean; "
              "miter -equiv -flatten -make_assert -ignore_gold_x plic_ref plic_target m; "
              "hierarchy -top m; sat -verify -prove-asserts m")
    r = subprocess.run([y, "-q", "-p", script], capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
