// Copyright (C) 2026 Tim Michals
// SPDX-License-Identifier: GPL-3.0-or-later

`default_nettype none

// Core registers, one AXI4-Lite slave for two windows: trace (0x2000) and core (0x3000).
// Offsets below are relative to 0x2000.
//   0x0000 TRACE_MASK, 0x0004 TRACE_LEVEL, 0x0008 TRACE_DROPPED, 0x000C TRACE_MERGED, 0x0010 TIMESTAMP
//   0x1000 ID, 0x1004 VERSION, 0x1008 ISR (W1C), 0x100C IMR, 0x1010 STREAM_PEND, 0x1014 DMA_DONE (W1C),
//   0x1018 DMA_ERR (W1C), 0x101C IN_DROPPED
// @impl [SPEC-ICN-11] [SPEC-TRC-02] [SPEC-LINK-05] docs/INTERCONNECT_SPECIFICATION.md
module pio2_core_regs (
    input  wire          clk,
    input  wire          rst_n,

    input  wire  [31:0]  s_axil_awaddr,
    input  wire          s_axil_awvalid,
    output logic         s_axil_awready,
    input  wire  [31:0]  s_axil_wdata,
    input  wire  [3:0]   s_axil_wstrb,
    input  wire          s_axil_wvalid,
    output logic         s_axil_wready,
    output logic [1:0]   s_axil_bresp,
    output logic         s_axil_bvalid,
    input  wire          s_axil_bready,
    input  wire  [31:0]  s_axil_araddr,
    input  wire          s_axil_arvalid,
    output logic         s_axil_arready,
    output logic [31:0]  s_axil_rdata,
    output logic [1:0]   s_axil_rresp,
    output logic         s_axil_rvalid,
    input  wire          s_axil_rready,

    // trace
    output logic [31:0]  o_trace_mask,
    input  wire  [15:0]  i_trace_level,
    input  wire  [31:0]  i_trace_dropped,      // saturating totals
    input  wire  [31:0]  i_trace_merged,
    input  wire  [31:0]  i_timestamp,

    // DMA (bit c-1 = channel c)
    input  wire  [8:0]   i_dma_done,           // pulses
    input  wire  [8:0]   i_dma_err,            // pulses
    input  wire  [8:0]   i_stream_pend,        // levels
    input  wire          i_in_drop,            // pulse
    output logic [8:0]   o_done_flags,

    input  wire          i_pio_irq,
    output logic         o_irq
);
    localparam logic [31:0] ID      = 32'h5049_4F32;     // "PIO2"
    localparam logic [31:0] VERSION = 32'h0001_0000;

    logic        wr, rd;
    logic [12:0] waddr, raddr;
    logic [31:0] wdata, rdata;

    pio2_axil_regs #(.AW(13)) u_regs (
        .clk(clk), .rst_n(rst_n),
        .s_axil_awaddr(s_axil_awaddr), .s_axil_awvalid(s_axil_awvalid), .s_axil_awready(s_axil_awready),
        .s_axil_wdata(s_axil_wdata), .s_axil_wstrb(s_axil_wstrb), .s_axil_wvalid(s_axil_wvalid),
        .s_axil_wready(s_axil_wready), .s_axil_bresp(s_axil_bresp), .s_axil_bvalid(s_axil_bvalid),
        .s_axil_bready(s_axil_bready), .s_axil_araddr(s_axil_araddr), .s_axil_arvalid(s_axil_arvalid),
        .s_axil_arready(s_axil_arready), .s_axil_rdata(s_axil_rdata), .s_axil_rresp(s_axil_rresp),
        .s_axil_rvalid(s_axil_rvalid), .s_axil_rready(s_axil_rready),
        .o_wr(wr), .o_waddr(waddr), .o_wdata(wdata), .o_rd(rd), .o_raddr(raddr), .i_rdata(rdata)
    );

    logic [31:0] dropped_base, merged_base, dropped_q, in_dropped;
    logic [8:0]  isr, imr, err_flags;
    wire  [8:0]  w1c = (wr && waddr == 13'h1008) ? wdata[8:0] : 9'd0;

    // Causes: levels set their bit again while true, so a W1C never loses one ([SPEC-LINK-05]).
    wire  [8:0]  cause = {i_pio_irq, i_trace_dropped != dropped_q, 2'b00, |err_flags, |o_done_flags,
                          |i_stream_pend, 2'b00};

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            o_trace_mask <= '0; dropped_base <= '0; merged_base <= '0; dropped_q <= '0;
            in_dropped <= '0; isr <= '0; imr <= '0; o_done_flags <= '0; err_flags <= '0;
        end else begin
            dropped_q <= i_trace_dropped;
            isr <= (isr & ~w1c) | cause;
            o_done_flags <= (o_done_flags & ~((wr && waddr == 13'h1014) ? wdata[8:0] : 9'd0)) | i_dma_done;
            err_flags    <= (err_flags    & ~((wr && waddr == 13'h1018) ? wdata[8:0] : 9'd0)) | i_dma_err;
            if (wr && waddr == 13'h101C)  in_dropped <= '0;
            else if (i_in_drop && !(&in_dropped)) in_dropped <= in_dropped + 32'd1;
            if (wr) begin
                case (waddr)
                    13'h0000: o_trace_mask <= wdata;
                    13'h0008: dropped_base <= i_trace_dropped;
                    13'h000C: merged_base  <= i_trace_merged;
                    13'h100C: imr          <= wdata[8:0];
                    default: ;
                endcase
            end
        end
    end

    always_comb begin
        case (raddr)
            13'h0000: rdata = o_trace_mask;
            13'h0004: rdata = {16'd0, i_trace_level};
            13'h0008: rdata = i_trace_dropped - dropped_base;
            13'h000C: rdata = i_trace_merged - merged_base;
            13'h0010: rdata = i_timestamp;
            13'h1000: rdata = ID;
            13'h1004: rdata = VERSION;
            13'h1008: rdata = {23'd0, isr};
            13'h100C: rdata = {23'd0, imr};
            13'h1010: rdata = {23'd0, i_stream_pend};
            13'h1014: rdata = {23'd0, o_done_flags};
            13'h1018: rdata = {23'd0, err_flags};
            13'h101C: rdata = in_dropped;
            default:  rdata = 32'd0;
        endcase
    end

    assign o_irq = |(isr & imr);

    /* verilator lint_off UNUSEDSIGNAL */
    wire _unused = &{1'b0, rd};
    /* verilator lint_on UNUSEDSIGNAL */
endmodule

`default_nettype wire
