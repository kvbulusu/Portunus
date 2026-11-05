set P $::env(T198_PLATFORM)
read_lef $P/lef/asap7_tech_1x_201209.lef
read_lef $P/lef/asap7sc7p5t_28_R_1x_220121a.lef
read_lef big.lef
foreach l {SEQ_RVT_FF_nldm_220123.lib SIMPLE_RVT_FF_nldm_211120.lib.gz INVBUF_RVT_FF_nldm_220122.lib.gz AO_RVT_FF_nldm_211120.lib.gz} { read_liberty [glob $P/lib/NLDM/asap7sc7p5t_$l] }
read_liberty big.lib
read_verilog t.v
link_design t
initialize_floorplan -die_area "0 0 600 600" -core_area "5 5 595 595" -site asap7sc7p5t
source $P/openRoad/make_tracks.tcl
place_pins -hor_layers M4 -ver_layers M5
set b [ord::get_db_block]
set m [$b findInst u_big]; $m setLocation 100000 100000; $m setPlacementStatus FIRM
cut_rows -halo_width_x 0 -halo_width_y 0
# start regs NW of macro (above top edge, west), end regs east of macro (low)
for {set i 0} {$i < 8} {incr i} {
  [$b findInst s_r$i] setLocation [expr {110000 + $i*2000}] 510000; [$b findInst s_r$i] setPlacementStatus PLACED
  [$b findInst e_r$i] setLocation 520000 [expr {120000 + $i*2000}]; [$b findInst e_r$i] setPlacementStatus PLACED
}
[$b findInst ux] setLocation 540000 120000; [$b findInst ux] setPlacementStatus PLACED
detailed_placement
source $P/setRC.tcl
create_clock -period 500 [get_ports clk]
estimate_parasitics -placement
proc inmac { } { global b; set n 0; foreach i [$b getInsts] { if {[[$i getMaster] isBlock]} continue; lassign [$i getLocation] x y; if {$x>100000 && $x<500000 && $y>100000 && $y<500000} {incr n} }; return $n }
source $::env(T210_HOOK_PRE)
repair_design -max_wire_length 60
puts "T210 after_rsz buffers_in_macro [inmac] insts [llength [$b getInsts]]"
source $::env(T210_HOOK_POST)
catch {detailed_placement} e
puts "T210 dpl '$e' buffers_in_macro [inmac]"
estimate_parasitics -placement
set p [lindex [find_timing_paths -path_delay max] 0]; puts "T210 wns_dpl [get_property $p slack]"
if {[info exists ::env(T210_HOOK_DPL)]} { source $::env(T210_HOOK_DPL) }
puts "T210 final buffers_in_macro [inmac] insts [llength [$b getInsts]]"
catch {check_placement} e2; puts "T210 check_placement '$e2'"
report_checks -path_delay max -format end -group_path_count 3
set p [lindex [find_timing_paths -path_delay max] 0]; puts "T210 wns [get_property $p slack]"
