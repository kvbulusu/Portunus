# test_220 extension of fixtures/t219a/mock_openroad.tcl (loaded via MOCK_EXT):
# adds a u_noc macro whose clk pin hangs on a 6-stage janus_maltrimbuf chain
# below u_clkgen/_146_, odb connect/disconnect/destroy, and a toy STA where
# every bypassed trim stage moves the u_noc clock 31.5 ps earlier. Proves the
# §220 hook + probe section M execute; says nothing about real numbers.
set M(noc) [mkmaster axi4_crossbar {clk in {m_i[283]} in {m_i[280]} in {s_o[514]} out} 1]
set M(bufx24) [mkmaster BUFx24_ASAP7_75t_R {A in Y out}]
proc build_noc { } {
  global M
  set noc [mkinst u_noc $M(noc) 200000 200000]
  set root [mkinst u_clkgen/_146_ $M(bufx24) 150000 150000]
  set up [it $root Y]
  set prev ""
  for { set i 6 } { $i >= 1 } { incr i -1 } {
    set b [mkinst janus_maltrimbuf9600$i $M(bufx24) [expr {160000 + 5000 * (6 - $i)}] 190000]
    mknet [expr {$i == 6 ? "r0" : "janus_maltrimnet9600[expr {$i + 1}]"}] $up [it $b A]
    set up [it $b Y]
  }
  mknet janus_maltrimnet96001 $up [it $noc clk]
  set u [mkinst "u_ucut_m/out_q\[12\]" $M(dff) 190000 200000]
  mknet mi283 [it $u QN] [it $noc {m_i[283]}]
}
build_noc

rename obj_call _mock_obj_call0
proc obj_call { h m args } {
  switch -- [f $h kind]:$m {
    iterm:disconnect {
      set n [f $h net]
      if { $n ne "NULL" } { fset $n iterms [lsearch -all -inline -not -exact [f $n iterms] $h] }
      fset $h net NULL
      return
    }
    iterm:connect {
      set n [lindex $args 0]
      fset $h net $n
      fset $n iterms [concat [f $n iterms] [list $h]]
      return
    }
    default { return [_mock_obj_call0 $h $m {*}$args] }
  }
}
namespace eval odb {}
proc odb::dbInst_destroy { i } {
  foreach t [f $i iterms] { $t disconnect }
  fset $::BLOCK insts [lsearch -all -inline -not -exact [f $::BLOCK insts] $i]
  lappend ::mock_destroyed [f $i name]
}
proc odb::dbNet_destroy { n } {}
proc repair_clock_nets { args } { lappend ::mock_rcn 1 }

# trim stages bypassed (6 - stages still between u_noc/clk and r0)
proc mock_bypass { } {
  set it [[$::BLOCK findInst u_noc] findITerm clk]
  set d 0
  set net [f $it net]
  while { 1 } {
    set drv ""
    foreach t [f $net iterms] { if { [$t isOutputSignal] } { set drv $t } }
    if { $drv eq "" || ![string match janus_maltrim* [[$drv getInst] getName]] } { break }
    incr d
    set net [[[$drv getInst] findITerm A] getNet]
  }
  return [expr {6 - $d}]
}
proc sta::time_sta_ui { v } { return $v }
proc sta::worst_slack_cmd { mm } {
  set b [mock_bypass]
  if { $mm eq "min" } { return [expr {min(-157.3 + 31.5 * $b, -60.0)}] }
  return [expr {$b > 4 ? -304.5 - 30.0 * ($b - 4) : -304.5}]
}
proc sta::total_negative_slack_cmd { mm } {
  set b [mock_bypass]
  if { $mm eq "min" } { return [expr {min(-2000.0 + 300.0 * $b, -200.0)}] }
  return [expr {$b > 4 ? -144557.0 : -144057.0}]
}
rename get_property _mock_get_property0
proc get_property { o p } {
  if { [string match P:u_noc/* $o] } {
    set b [mock_bypass]
    switch -glob -- $o:$p {
      *m_i?283?:slack_min { return [expr {-157.3 + 31.5 * $b}] }
      *m_i?280?:slack_min { return [expr {-152.8 + 31.5 * $b}] }
      *:slack_max { return 49.4 }
      *clk:arrival_* { return [expr {3317.1 - 31.5 * $b}] }
    }
  }
  return [_mock_get_property0 $o $p]
}
rename get_pins _mock_get_pins0
proc get_pins { args } {
  if { [lsearch -glob $args u_noc/m_i*] >= 0 } { return [list {P:u_noc/m_i[283]} {P:u_noc/m_i[280]}] }
  return [_mock_get_pins0 {*}$args]
}
rename find_timing_paths _mock_ftp0
proc find_timing_paths { args } {
  set k [lsearch -exact $args -to]
  if { $k >= 0 && [string match *u_noc/m_i* [lindex $args [expr {$k + 1}]]] } {
    set b [mock_bypass]
    return [list "PE:u_ucut_m/out_q\[12\]/CLK|u_noc/m_i\[283\]|[expr {-157.3 + 31.5 * $b}]" \
                 "PE:u_ucut_m/out_q\[9\]/CLK|u_noc/m_i\[280\]|[expr {-152.8 + 31.5 * $b}]"]
  }
  return [_mock_ftp0 {*}$args]
}
