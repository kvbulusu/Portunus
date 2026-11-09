# test_224 driver run under mock_openroad.tcl: source the §224 hook, run it,
# report the u_noc/clk chain depth left + destroyed trim buffers.
set hooks [file normalize [file join [file dirname [info script]] .. .. .. .. scripts hooks]]
source [file join $hooks janus_cts_leaf_retap.tcl]
source [file join $hooks janus_cts_macro_retrim.tcl]
source [file join $hooks janus_cts_noc_hold_bal.tcl]
janus_cts_noc_hold_bal
puts "NHB_BYPASS=[mock_bypass]"
puts "NHB_DESTROYED=[expr {[info exists ::mock_destroyed] ? [llength $::mock_destroyed] : 0}]"
exit
