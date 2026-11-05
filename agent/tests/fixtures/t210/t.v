module t(input clk, input d, output q);
BIGM u_big (.a(d));
wire s0, e0; DFFHQNx1_ASAP7_75t_R s_r0 (.CLK(clk), .D(d), .QN(s0)); DFFHQNx1_ASAP7_75t_R e_r0 (.CLK(clk), .D(s0), .QN(e0));
wire s1, e1; DFFHQNx1_ASAP7_75t_R s_r1 (.CLK(clk), .D(d), .QN(s1)); DFFHQNx1_ASAP7_75t_R e_r1 (.CLK(clk), .D(s1), .QN(e1));
wire s2, e2; DFFHQNx1_ASAP7_75t_R s_r2 (.CLK(clk), .D(d), .QN(s2)); DFFHQNx1_ASAP7_75t_R e_r2 (.CLK(clk), .D(s2), .QN(e2));
wire s3, e3; DFFHQNx1_ASAP7_75t_R s_r3 (.CLK(clk), .D(d), .QN(s3)); DFFHQNx1_ASAP7_75t_R e_r3 (.CLK(clk), .D(s3), .QN(e3));
wire s4, e4; DFFHQNx1_ASAP7_75t_R s_r4 (.CLK(clk), .D(d), .QN(s4)); DFFHQNx1_ASAP7_75t_R e_r4 (.CLK(clk), .D(s4), .QN(e4));
wire s5, e5; DFFHQNx1_ASAP7_75t_R s_r5 (.CLK(clk), .D(d), .QN(s5)); DFFHQNx1_ASAP7_75t_R e_r5 (.CLK(clk), .D(s5), .QN(e5));
wire s6, e6; DFFHQNx1_ASAP7_75t_R s_r6 (.CLK(clk), .D(d), .QN(s6)); DFFHQNx1_ASAP7_75t_R e_r6 (.CLK(clk), .D(s6), .QN(e6));
wire s7, e7; DFFHQNx1_ASAP7_75t_R s_r7 (.CLK(clk), .D(d), .QN(s7)); DFFHQNx1_ASAP7_75t_R e_r7 (.CLK(clk), .D(s7), .QN(e7));
XOR2xp5_ASAP7_75t_R ux (.A(e0), .B(e1), .Y(q));
endmodule
