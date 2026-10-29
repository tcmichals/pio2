// Copyright (C) 2026 Tim Michals
// SPDX-License-Identifier: GPL-3.0-or-later
`default_nettype none
// Formal harness: ZipCPU's faxil_slave properties (third_party/wb2axip, Apache-2.0) on the PIO tile's
// AXI4-Lite port ([SPEC-AXI-04], [SPEC-VER-01]). faxil_slave assumes legal master behaviour and asserts
// that the tile answers by the AXI4-Lite rules; everything else (pins, timestamp, TLP inputs) is free.
// @verifies [SPEC-AXI-04] docs/INTERCONNECT_SPECIFICATION.md
module pio_tile_axil (
    input wire        clk,
    input wire        rst_n,
    input wire [63:0] i_sys_timestamp,
    input wire [31:0] i_gpio,
    input wire [31:0] s_axil_awaddr,
    input wire        s_axil_awvalid,
    input wire [31:0] s_axil_wdata,
    input wire [3:0]  s_axil_wstrb,
    input wire        s_axil_wvalid,
    input wire        s_axil_bready,
    input wire [31:0] s_axil_araddr,
    input wire        s_axil_arvalid,
    input wire        s_axil_rready,
    input wire [511:0] s_tlp_tdata,
    input wire        s_tlp_tvalid,
    input wire        m_tlp_tready
);
    wire        awready, wready, bvalid, arready, rvalid;
    wire [1:0]  bresp, rresp;
    wire [31:0] rdata;
    wire [31:0] gpio_out, gpio_dir;
    wire        s_tlp_tready, m_tlp_tvalid, irq;
    wire [511:0] m_tlp_tdata;

    asp_pio_tile dut (
        .clk(clk), .rst_n(rst_n), .i_sys_timestamp(i_sys_timestamp), .i_gpio(i_gpio),
        .o_gpio_out(gpio_out), .o_gpio_pindir(gpio_dir),
        .s_axil_awaddr(s_axil_awaddr), .s_axil_awvalid(s_axil_awvalid), .s_axil_awready(awready),
        .s_axil_wdata(s_axil_wdata), .s_axil_wstrb(s_axil_wstrb), .s_axil_wvalid(s_axil_wvalid),
        .s_axil_wready(wready), .s_axil_bresp(bresp), .s_axil_bvalid(bvalid), .s_axil_bready(s_axil_bready),
        .s_axil_araddr(s_axil_araddr), .s_axil_arvalid(s_axil_arvalid), .s_axil_arready(arready),
        .s_axil_rdata(rdata), .s_axil_rresp(rresp), .s_axil_rvalid(rvalid), .s_axil_rready(s_axil_rready),
        .s_tlp_tdata(s_tlp_tdata), .s_tlp_tvalid(s_tlp_tvalid), .s_tlp_tready(s_tlp_tready),
        .m_tlp_tdata(m_tlp_tdata), .m_tlp_tvalid(m_tlp_tvalid), .m_tlp_tready(m_tlp_tready),
        .o_pio_irq(irq)
    );

    wire [3:0] f_rd, f_wr, f_awr;
    faxil_slave #(
        .C_AXI_DATA_WIDTH(32), .C_AXI_ADDR_WIDTH(32),
        .F_OPT_ASYNC_RESET(1'b1), .F_OPT_NO_RESET(1'b0),
        .F_LGDEPTH(4), .F_AXI_MAXWAIT(12), .F_AXI_MAXRSTALL(12), .F_AXI_MAXDELAY(12)
    ) props (
        .i_clk(clk), .i_axi_reset_n(rst_n),
        .i_axi_awvalid(s_axil_awvalid), .i_axi_awready(awready), .i_axi_awaddr(s_axil_awaddr), .i_axi_awprot(3'b000),
        .i_axi_wvalid(s_axil_wvalid), .i_axi_wready(wready), .i_axi_wdata(s_axil_wdata), .i_axi_wstrb(s_axil_wstrb),
        .i_axi_bvalid(bvalid), .i_axi_bready(s_axil_bready), .i_axi_bresp(bresp),
        .i_axi_arvalid(s_axil_arvalid), .i_axi_arready(arready), .i_axi_araddr(s_axil_araddr), .i_axi_arprot(3'b000),
        .i_axi_rvalid(rvalid), .i_axi_rready(s_axil_rready), .i_axi_rdata(rdata), .i_axi_rresp(rresp),
        .f_axi_rd_outstanding(f_rd), .f_axi_wr_outstanding(f_wr), .f_axi_awr_outstanding(f_awr)
    );
    wire _unused = &{1'b0, gpio_out, gpio_dir, s_tlp_tready, m_tlp_tvalid, m_tlp_tdata, irq, f_rd, f_wr, f_awr};
endmodule
`default_nettype wire
