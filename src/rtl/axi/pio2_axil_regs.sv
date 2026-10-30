// Copyright (C) 2026 Tim Michals
// SPDX-License-Identifier: GPL-3.0-or-later

`default_nettype none

// AXI4-Lite slave front end for pio2's register blocks (DMA, trace, core registers).
//
// One write and one read per clock (back to back, no idle cycle). A write is taken when AW and W
// are both valid and the B channel is free (or being emptied in this clock); a read when AR is
// valid and the R channel is free. The register block sees one-clock strobes:
//   o_wr  with o_waddr / o_wdata: write this register now (only for WSTRB == 4'hF);
//   o_rd  with o_raddr: read side effects happen now, i_rdata (combinational) is captured now.
// Whole words only: a write with WSTRB other than 4'hF changes nothing and answers SLVERR.
// @impl [SPEC-AXI-01] [SPEC-AXI-03] docs/INTERCONNECT_SPECIFICATION.md
module pio2_axil_regs #(
    parameter int AW = 12                  // register offset bits inside the window
) (
    input  wire           clk,
    input  wire           rst_n,

    input  wire  [31:0]   s_axil_awaddr,
    input  wire           s_axil_awvalid,
    output logic          s_axil_awready,
    input  wire  [31:0]   s_axil_wdata,
    input  wire  [3:0]    s_axil_wstrb,
    input  wire           s_axil_wvalid,
    output logic          s_axil_wready,
    output logic [1:0]    s_axil_bresp,
    output logic          s_axil_bvalid,
    input  wire           s_axil_bready,
    input  wire  [31:0]   s_axil_araddr,
    input  wire           s_axil_arvalid,
    output logic          s_axil_arready,
    output logic [31:0]   s_axil_rdata,
    output logic [1:0]    s_axil_rresp,
    output logic          s_axil_rvalid,
    input  wire           s_axil_rready,

    output logic          o_wr,
    output logic [AW-1:0] o_waddr,
    output logic [31:0]   o_wdata,
    output logic          o_rd,
    output logic [AW-1:0] o_raddr,
    input  wire  [31:0]   i_rdata
);
    wire b_free = !s_axil_bvalid || s_axil_bready;
    wire r_free = !s_axil_rvalid || s_axil_rready;
    wire take_w = s_axil_awvalid && s_axil_wvalid && b_free;

    assign s_axil_awready = take_w;
    assign s_axil_wready  = take_w;
    assign s_axil_arready = r_free;

    assign o_wr    = take_w && s_axil_wstrb == 4'hF;
    assign o_waddr = s_axil_awaddr[AW-1:0];
    assign o_wdata = s_axil_wdata;
    assign o_rd    = s_axil_arvalid && r_free;
    assign o_raddr = s_axil_araddr[AW-1:0];

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            s_axil_bvalid <= 1'b0;
            s_axil_bresp  <= 2'b00;
            s_axil_rvalid <= 1'b0;
            s_axil_rresp  <= 2'b00;
            s_axil_rdata  <= '0;
        end else begin
            if (take_w) begin
                s_axil_bvalid <= 1'b1;
                s_axil_bresp  <= s_axil_wstrb == 4'hF ? 2'b00 : 2'b10;
            end else if (s_axil_bready) begin
                s_axil_bvalid <= 1'b0;
            end
            if (o_rd) begin
                s_axil_rvalid <= 1'b1;
                s_axil_rresp  <= 2'b00;
                s_axil_rdata  <= i_rdata;
            end else if (s_axil_rready) begin
                s_axil_rvalid <= 1'b0;
            end
        end
    end

    /* verilator lint_off UNUSEDSIGNAL */
    wire _unused = &{1'b0, s_axil_awaddr[31:AW], s_axil_araddr[31:AW]};
    /* verilator lint_on UNUSEDSIGNAL */
endmodule

`default_nettype wire
