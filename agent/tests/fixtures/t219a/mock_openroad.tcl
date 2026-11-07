# Minimal OpenROAD/odb/STA mock for test_219a: lets plain tclsh execute every
# top-level path of soc_hold_probe.tcl on a tiny fake design. It only proves
# the Tcl is executable (no unknown procs, no bracket/format crashes, bounded
# output, nothing written); it says nothing about OpenROAD numbers.
# Usage: tclsh mock_openroad.tcl <probe.tcl>
set ::writes {}
set ::OBJ [dict create]
set ::nid 0
proc mk { kind fields } {
  set h "_${kind}[incr ::nid]"
  dict set ::OBJ $h [dict merge [dict create kind $kind] $fields]
  proc $h { m args } "obj_call $h \$m {*}\$args"
  return $h
}
proc f { h k } { return [dict get $::OBJ $h $k] }
proc fset { h k v } { dict set ::OBJ $h $k $v }
proc box { x0 y0 x1 y1 } { return [mk box [dict create x0 $x0 y0 $y0 x1 $x1 y1 $y1]] }
proc obj_call { h m args } {
  switch -- [f $h kind]:$m {
    box:xMin { return [f $h x0] }  box:yMin { return [f $h y0] }
    box:xMax { return [f $h x1] }  box:yMax { return [f $h y1] }
    box:ll { return [list [f $h x0] [f $h y0]] }
    box:ur { return [list [f $h x1] [f $h y1]] }
    block:getDbUnitsPerMicron { return 1000 }
    block:getInsts { return [f $h insts] }
    block:getDieArea { return [box 0 0 1000000 800000] }
    block:findInst {
      foreach i [f $h insts] { if { [f $i name] eq [lindex $args 0] } { return $i } }
      return NULL
    }
    block:findBTerm { return NULL }
    inst:getName { return [f $h name] }
    inst:getMaster { return [f $h master] }
    inst:getBBox { return [box [f $h x] [f $h y] [expr {[f $h x] + 200}] [expr {[f $h y] + 270}]] }
    inst:getLocation { return [list [f $h x] [f $h y]] }
    inst:getOrient { return R0 }
    inst:getITerms { return [f $h iterms] }
    inst:findITerm {
      foreach it [f $h iterms] { if { [f [f $it mterm] name] eq [lindex $args 0] } { return $it } }
      return NULL
    }
    master:getName { return [f $h name] }
    master:isBlock { return [f $h block] }
    master:findMTerm {
      foreach mt [f $h mterms] { if { [f $mt name] eq [lindex $args 0] } { return $mt } }
      return NULL
    }
    mterm:getName { return [f $h name] }
    iterm:getInst { return [f $h inst] }
    iterm:getMTerm { return [f $h mterm] }
    iterm:getNet { return [f $h net] }
    iterm:isOutputSignal { return [expr {[f [f $h mterm] dir] eq "out"}] }
    iterm:isInputSignal { return [expr {[f [f $h mterm] dir] eq "in"}] }
    iterm:getAvgXY { set i [f $h inst]; return [list 1 [f $i x] [f $i y]] }
    net:getName { return [f $h name] }
    net:getITerms { return [f $h iterms] }
    net:getBTerms { return {} }
    net:getSigType { return SIGNAL }
    default { error "mock: no method [f $h kind]:$m" }
  }
}
# --- masters --------------------------------------------------------------------
proc mkmaster { name pins {block 0} } {
  set mts {}
  foreach {p d} $pins { lappend mts [mk mterm [dict create name $p dir $d]] }
  return [mk master [dict create name $name mterms $mts block $block]]
}
set M(dff) [mkmaster DFFASRHQNx1_ASAP7_75t_SL {CLK in D in QN out}]
set M(buf) [mkmaster BUFx6f_ASAP7_75t_SL {A in Y out}]
set M(hb)  [mkmaster HB1xp67_ASAP7_75t_SL {A in Y out}]
set M(agent) [mkmaster agent_cluster_top {{npu_s_job_i[12]} in {npu_s_job_i[13]} in clk_i in} 1]
set ::BLOCK [mk block [dict create insts {}]]
proc mkinst { name m x y } {
  set i [mk inst [dict create name $name master $m x $x y $y iterms {}]]
  set its {}
  foreach mt [f $m mterms] { lappend its [mk iterm [dict create inst $i mterm $mt net NULL]] }
  fset $i iterms $its
  fset $::BLOCK insts [concat [f $::BLOCK insts] [list $i]]
  return $i
}
proc it { inst pin } { return [$inst findITerm $pin] }
proc mknet { name args } {
  set n [mk net [dict create name $name iterms $args]]
  foreach i $args { fset $i net $n }
  return $n
}
proc build_design { } {
  global M
  set ag  [mkinst u_agent $M(agent) 10000 10000]
  set lf  [mkinst clkbuf_leaf_93_agent_clk_regs $M(buf) 400000 300000]
  set w   [mkinst wire96000 $M(buf) 300000 200000]
  set w2  [mkinst u_clkgen/wire95999 $M(buf) 310000 200000]
  foreach b {12 13} {
    set d($b) [mkinst "u_npu_s_job_cdc.data_o\[$b\]\$_DFF_PN0_" $M(dff) [expr {500000 + $b}] 300000]
    set s($b) [mkinst "u_npu_s_job_cdc.sync_q0\[$b\]\$_DFF_PN0_" $M(dff) [expr {510000 + $b}] 300000]
    mknet "n_s$b" [it $s($b) QN] [it $d($b) D]
  }
  mknet clk_leaf [it $lf Y] [it $d(12) CLK] [it $d(13) CLK] [it $s(12) CLK] [it $s(13) CLK]
  mknet q12 [it $d(12) QN] [it $w A]
  mknet q12b [it $w Y] [it $ag {npu_s_job_i[12]}]
  mknet q13 [it $d(13) QN] [it $ag {npu_s_job_i[13]}]
  set pr [mkinst u_periph/r_q $M(dff) 600000 100000]
  set kv [mkinst u_kv_secure_subsystem/k_q $M(dff) 700000 100000]
  mknet g1 [it $pr QN] [it $kv D]
}
build_design

# --- ORFS / OpenROAD / STA stubs ------------------------------------------------------
proc load_design { args } {}
proc read_spef { args } {}
proc estimate_parasitics { args } {}
proc set_propagated_clock { args } {}
proc set_debug_level { args } { lappend ::mock_dbg $args }
proc set_dont_use { args } {}
proc all_clocks { } { return {CLK:clk_user CLK:clk_agent} }
namespace eval ord {}
proc ord::get_db_block { } { return $::BLOCK }
namespace eval sta {}
proc sta::find_pin { n } {
  set k [string last / $n]
  set i [$::BLOCK findInst [string range $n 0 [expr {$k - 1}]]]
  if { $i eq "NULL" } { return "" }
  if { [$i findITerm [string range $n [expr {$k + 1}] end]] eq "NULL" } { return "" }
  return "P:$n"
}
proc get_pins { args } { return "" }
proc get_full_name { o } { return [string range $o 2 end] }
proc get_name { o } { return [lindex [split $o :] 1] }
proc sta::with_output_to_variable { var cmd } {
  upvar 1 $var v
  set v [uplevel 1 $cmd]
}
proc report_checks { args } { return "mock report_checks $args\nline2" }
proc report_worst_slack { args } { return "worst slack -1.0" }
proc find_timing_paths { args } {
  set a [dict create]
  for { set i 0 } { $i < [llength $args] } { incr i } {
    set k [lindex $args $i]
    if { [string match -* $k] } { dict set a $k [lindex $args [incr i]] }
  }
  if { [dict exists $a -slack_max] } {
    return [list "PE:u_periph/r_q/CLK|u_kv_secure_subsystem/k_q/D|-40.5"]
  }
  if { [dict exists $a -from] } {
    set p [get_full_name [dict get $a -from]]
    return [list "PE:$p|x/D|-3.0"]
  }
  if { [dict exists $a -to] } {
    return [list "PE:u_npu_s_job_cdc.sync_q0\[12\]\$_DFF_PN0_/CLK|[get_full_name [dict get $a -to]]|-99.0"]
  }
  return {}
}
proc get_property { o p } {
  switch -glob -- $o {
    PE:* {
      lassign [split [string range $o 3 end] |] sp ep sl
      switch -- $p {
        startpoint { return "P:$sp" }
        endpoint { return "P:$ep" }
        slack { return $sl }
        startpoint_clock - endpoint_clock { return CLK:clk_user }
        points { return [list "T:$sp|812.5" "T:$ep|900.0"] }
      }
    }
    T:* {
      lassign [split [string range $o 2 end] |] pin arr
      switch -- $p { pin { return "P:$pin" } arrival { return $arr } }
    }
    P:* {
      switch -- $p {
        slack_min { return -99.0 }
        slack_max { return [expr {[string match *k_q* $o] ? 20.0 : 150.0}] }
      }
    }
  }
  error "mock get_property $o $p"
}
namespace eval utl {}
proc utl::redirect_file_begin { f } { set ::redir $f }
proc utl::redirect_file_end { } { unset ::redir }
proc repair_timing { args } {
  set ::rt_args $args
  if { [info exists ::redir] } {
    set fh [open $::redir w]
    puts $fh {repair_hold: header}
    for { set i 0 } { $i < 500 } { incr i } {
      puts $fh "repair_hold: endpoint u_npu_s_job_cdc.data_o\\\[12\\\]\$_DFF_PN0_/D setup slack 10 rejected $i"
      puts $fh "repair_hold: other u_x/y_$i slew"
    }
    puts $fh {[WARNING RSZ-0066] Unable to repair all hold violations.}
    close $fh
  }
  mkinst hold1 $::M(hb) 1 1
}
rename exit ::_real_exit
proc exit { args } { puts "MOCK_EXIT"; flush stdout; ::_real_exit 0 }
# trap any write attempt at the Tcl level
foreach w {write_db orfs_write_db write_def write_verilog write_spef write_sdc} {
  proc $w { args } "lappend ::writes $w; error {mock: $w called}"
}
if { [catch { source [lindex $argv 0] } e] } {
  puts stderr "MOCK_ERROR: $::errorInfo"
  ::_real_exit 3
}
puts "MOCK_FELL_THROUGH"
::_real_exit 4
