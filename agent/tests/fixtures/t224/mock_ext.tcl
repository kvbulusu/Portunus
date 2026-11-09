# test_224 extension (MOCK_EXT) of fixtures/t219a/mock_openroad.tcl: reuses the
# t220 u_noc + 6-stage janus_maltrimbuf chain toy STA and adds get_cells,
# get_pins -of_objects, pin direction and u_noc output-pin hold (s_o[514]:
# 200 - 31.5 * stages bypassed). Proves the §224 hook executes; no real numbers.
source [file join [file dirname [info script]] .. t220 mock_ext.tcl]
proc get_cells { args } { return [list "C:[lindex $args end]"] }
rename get_pins _mock_get_pins_t220
proc get_pins { args } {
  set k [lsearch -exact $args -of_objects]
  if { $k >= 0 } {
    set c [string range [lindex [lindex $args [expr {$k + 1}]] 0] 2 end]
    set r {}
    foreach t [[$::BLOCK findInst $c] getITerms] { lappend r "P:$c/[f [f $t mterm] name]" }
    return $r
  }
  return [_mock_get_pins_t220 {*}$args]
}
rename get_property _mock_get_property_t220
proc get_property { o p } {
  if { [string match P:u_noc/* $o] } {
    if { $p eq "direction" } {
      set n [string range $o 8 end]
      set t [[$::BLOCK findInst u_noc] findITerm $n]
      return [expr {[f [f $t mterm] dir] eq "in" ? "input" : "output"}]
    }
    if { [string match *s_o* $o] && $p eq "slack_min" } { return [expr {200.0 - 31.5 * [mock_bypass]}] }
  }
  return [_mock_get_property_t220 $o $p]
}
