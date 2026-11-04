set P $::env(T198_PLATFORM)
read_lef $P/lef/asap7_tech_1x_201209.lef
read_lef $P/lef/asap7sc7p5t_28_R_1x_220121a.lef
read_lef m.lef
foreach l {SEQ_RVT_FF_nldm_220123.lib SIMPLE_RVT_FF_nldm_211120.lib.gz INVBUF_RVT_FF_nldm_220122.lib.gz AO_RVT_FF_nldm_211120.lib.gz} { read_liberty [glob $P/lib/NLDM/asap7sc7p5t_$l] }
read_liberty mu.lib
read_liberty m2.lib
read_verilog t.v
link_design t
initialize_floorplan -die_area "0 0 300 300" -core_area "5 5 295 295" -site asap7sc7p5t
source $P/openRoad/make_tracks.tcl
place_pins -hor_layers M4 -ver_layers M5
set b [ord::get_db_block]
foreach {n x y} {u_m 230 230 u_m2 60 220} { set i [$b findInst $n]; $i setLocation [expr {$x*1000}] [expr {$y*1000}]; $i setPlacementStatus FIRM }
global_placement -skip_io -density 0.4
detailed_placement
create_clock -name clk_user -period 1000 [get_pins u_gate/Y]
create_clock -name clk_agent -period 2000 [get_pins u_clkgen/ag/Y]
set_sense -type clock -stop_propagation -clocks [get_clocks clk_user] [get_pins u_clkgen/ag/Y]
set_clock_groups -asynchronous -group {clk_user clk_agent}
set_false_path -hold -from [get_clocks clk_user] -to [get_clocks clk_agent]
set_false_path -hold -from [get_clocks clk_agent] -to [get_clocks clk_user]
set_max_delay -ignore_clock_latency 1000 -from [get_clocks clk_agent] -to [get_clocks clk_user]
set_max_delay -ignore_clock_latency 600 -from [get_clocks clk_user] -to [get_clocks clk_agent]
set_input_delay 100 -clock clk_user [get_ports {d fetch en}]
set_input_delay 100 -clock clk_agent -add_delay [get_ports fetch]
set_output_delay 100 -clock clk_user [get_ports {q q2}]
set_output_delay 100 -clock clk_agent [get_ports q3]
source $P/setRC.tcl
set_wire_rc -clock -layer M5
set ::env(CTS_BUF_LIST) BUFx4_ASAP7_75t_R
set ::env(JANUS_CTS_IO_REFPIN_MULTI) 1
set ::env(JANUS_CTS_IO_REFPIN_CLOCKS) "clk_user clk_agent"
set ::env(JANUS_CTS_IO_REFPIN_IN_clk_user) "d fetch en"
set ::env(JANUS_CTS_IO_REFPIN_IN_clk_agent) "fetch"
set ::env(JANUS_CTS_IO_REFPIN_OUT_clk_user) "q q2"
set ::env(JANUS_CTS_IO_REFPIN_OUT_clk_agent) "q3"
# §198f: janus_soc_top sets JANUS_CTS_XCUT_TCL (T198_XCUT=0 disables)
if { ![info exists ::env(T198_XCUT)] || $::env(T198_XCUT) ne "0" } {
  set ::env(JANUS_CTS_XCUT_TCL) [file join [file dirname $::env(T198_HOOK)] janus_soc_cts_xcut.tcl]
}
source $::env(T198_HOOK)
clock_tree_synthesis -sink_clustering_enable -repair_clock_nets -buf_list BUFx4_ASAP7_75t_R
set_propagated_clock [all_clocks]
estimate_parasitics -placement
proc ws { args } { set p [lindex [find_timing_paths {*}$args] 0]; if {$p eq ""} {return n/a}; return [format %.1f [get_property $p slack]] }
puts "T198D FINAL [janus_ofs_wns]"
foreach p {u_m/clk u_m2/clk r1/CLK a1/CLK u_clkgen/enq2/CLK u_clkgen/ag/A} { puts [format "T198D arr %s %.1f" $p [get_property [get_pins $p] arrival_max_rise]] }
puts "T198D selfloop u_m->u_m setup [ws -from [get_cells u_m] -to [get_pins u_m/din2] -path_delay max]"
puts "T198D u_m->rx setup [ws -from [get_cells u_m] -to [get_pins rx/D] -path_delay max] | r3->u_m hold [ws -to [get_pins u_m/din] -path_delay min]"
puts "T198D fetch->a1 setup [ws -from [get_ports fetch] -to [get_pins a1/D] -path_delay max] | r7->a1 setup [ws -from [get_pins r7/CLK] -to [get_pins a1/D] -path_delay max]"
puts "T198D u_m2 in hold [ws -to [get_pins u_m2/din] -path_delay min] out setup [ws -from [get_cells u_m2] -path_delay max]"
puts "T198D agent reg2reg setup [ws -from [get_pins a3/CLK] -to [get_pins a4/D] -path_delay max] user reg2reg hold [ws -from [get_pins r10/CLK] -to [get_pins r11/D] -path_delay min]"
puts "T198D d->r0 hold [ws -from [get_ports d] -path_delay min] q2 out setup [ws -to [get_ports q2] -path_delay max]"
