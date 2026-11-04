set P $::env(T198_PLATFORM)
read_lef $P/lef/asap7_tech_1x_201209.lef
read_lef $P/lef/asap7sc7p5t_28_R_1x_220121a.lef
read_lef m.lef
foreach l {SEQ_RVT_FF_nldm_220123.lib SIMPLE_RVT_FF_nldm_211120.lib.gz INVBUF_RVT_FF_nldm_220122.lib.gz AO_RVT_FF_nldm_211120.lib.gz} { read_liberty [glob $P/lib/NLDM/asap7sc7p5t_$l] }
read_liberty m1.lib
read_liberty m2.lib
read_verilog t.v
link_design t
initialize_floorplan -die_area "0 0 300 300" -core_area "5 5 295 295" -site asap7sc7p5t
source $P/openRoad/make_tracks.tcl
place_pins -hor_layers M4 -ver_layers M5
set b [ord::get_db_block]
foreach {n x y} {u_m 200 200 u_m2 60 220} { set i [$b findInst $n]; $i setLocation [expr {$x*1000}] [expr {$y*1000}]; $i setPlacementStatus FIRM }
global_placement -skip_io -density 0.4
detailed_placement
create_clock -name clk_user -period 1000 [get_pins u_gate/Y]
source $P/setRC.tcl
set_wire_rc -clock -layer M5
set ::env(CTS_BUF_LIST) BUFx4_ASAP7_75t_R
source $::env(T198_HOOK)
clock_tree_synthesis -sink_clustering_enable -repair_clock_nets -buf_list BUFx4_ASAP7_75t_R
set_propagated_clock [all_clocks]
estimate_parasitics -placement
puts "T198 FINAL [janus_ofs_wns]"
foreach p {u_m/clk u_m2/clk r1/CLK u_clkgen/enq/CLK} { puts "T198 arr $p [get_property [get_pins $p] arrival_max_rise]" }
