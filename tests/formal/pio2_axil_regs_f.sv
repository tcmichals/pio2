// Copyright (C) 2026 Tim Michals
// SPDX-License-Identifier: GPL-3.0-or-later
`default_nettype none
// Formal harness: ZipCPU's faxil_slave properties (third_party/wb2axip, Apache-2.0) on
// pio2_axil_regs, the AXI4-Lite front end of the DMA, trace and core registers ([SPEC-AXI-01],
// [SPEC-VER-01]). i_rdata (the register block's read mux) is free.
// @verifies [SPEC-AXI-01] docs/INTERCONNECT_SPECIFICATION.md
module pio2_axil_regs_f (
    input wire        clk,
    input wire        rst_n,
    input wire [31:0] s_axil_awaddr,
    input wire        s_axil_awvalid,
    input wire [31:0] s_axil_wdata,
    input wire [3:0]  s_axil_wstrb,
    input wire        s_axil_wvalid,
    input wire        s_axil_bready,
    input wire [31:0] s_axil_araddr,
    input wire        s_axil_arvalid,
    input wire        s_axil_rready,
    input wire [31:0] i_rdata
);
    wire        awready, wready, bvalid, arready, rvalid, wr, rd;
    wire [1:0]  bresp, rresp;
    wire [31:0] rdata, wdata;
    wire [11:0] waddr, raddr;

    pio2_axil_regs #(.AW(12)) dut (
        .clk(clk), .rst_n(rst_n),
        .s_axil_awaddr(s_axil_awaddr), .s_axil_awvalid(s_axil_awvalid), .s_axil_awready(awready),
        .s_axil_wdata(s_axil_wdata), .s_axil_wstrb(s_axil_wstrb), .s_axil_wvalid(s_axil_wvalid),
        .s_axil_wready(wready), .s_axil_bresp(bresp), .s_axil_bvalid(bvalid), .s_axil_bready(s_axil_bready),
        .s_axil_araddr(s_axil_araddr), .s_axil_arvalid(s_axil_arvalid), .s_axil_arready(arready),
        .s_axil_rdata(rdata), .s_axil_rresp(rresp), .s_axil_rvalid(rvalid), .s_axil_rready(s_axil_rready),
        .o_wr(wr), .o_waddr(waddr), .o_wdata(wdata), .o_rd(rd), .o_raddr(raddr), .i_rdata(i_rdata)
    );

    wire [4:0] f_rd, f_wr, f_awr;
    faxil_slave #(
        .C_AXI_DATA_WIDTH(32), .C_AXI_ADDR_WIDTH(32),
        .F_OPT_ASYNC_RESET(1'b1), .F_OPT_NO_RESET(1'b0),
        .F_LGDEPTH(5), .F_AXI_MAXWAIT(4), .F_AXI_MAXRSTALL(4), .F_AXI_MAXDELAY(4)
    ) props (
        .i_clk(clk), .i_axi_reset_n(rst_n),
        .i_axi_awvalid(s_axil_awvalid), .i_axi_awready(awready), .i_axi_awaddr(s_axil_awaddr), .i_axi_awprot(3'b000),
        .i_axi_wvalid(s_axil_wvalid), .i_axi_wready(wready), .i_axi_wdata(s_axil_wdata), .i_axi_wstrb(s_axil_wstrb),
        .i_axi_bvalid(bvalid), .i_axi_bready(s_axil_bready), .i_axi_bresp(bresp),
        .i_axi_arvalid(s_axil_arvalid), .i_axi_arready(arready), .i_axi_araddr(s_axil_araddr), .i_axi_arprot(3'b000),
        .i_axi_rvalid(rvalid), .i_axi_rready(s_axil_rready), .i_axi_rdata(rdata), .i_axi_rresp(rresp),
        .f_axi_rd_outstanding(f_rd), .f_axi_wr_outstanding(f_wr), .f_axi_awr_outstanding(f_awr)
    );

`ifdef FORMAL
    // Induction invariants, on ports only: at most one response of each kind is outstanding, and
    // AW / W are always taken together.
    always @(*) if (rst_n) begin
        assert(f_awr == {4'd0, bvalid});
        assert(f_wr  == {4'd0, bvalid});
        assert(f_rd  == {4'd0, rvalid});
        assert(rresp == 2'b00);
        assert(bresp == 2'b00 || bresp == 2'b10);
        // the register block is strobed exactly when a whole-word write is taken
        assert(wr == (s_axil_awvalid && s_axil_wvalid && awready && s_axil_wstrb == 4'hF));
        assert(rd == (s_axil_arvalid && arready));
    end
`endif
    wire _unused = &{1'b0, wdata, waddr, raddr};
endmodule
`default_nettype wire
