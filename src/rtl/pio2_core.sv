// Copyright (C) 2026 Tim Michals
// SPDX-License-Identifier: GPL-3.0-or-later

`default_nettype none

// pio2 core: the same on every board ([SPEC-ICN-11]). Four host-facing interfaces:
//   1. AXI4-Lite slave, all registers ([SPEC-AXI-03] map, Forencich axil_crossbar):
//      0x0000 tile, 0x1000 DMA, 0x2000 trace + 0x3000 core registers; DECERR elsewhere
//   2. AXI-Stream in, channel in TDEST (2..9) -> IN rings
//   3. AXI-Stream out, channel in TDEST (1..9): trace ring and OUT rings, whole TLAST blocks
//   4. o_irq = any (ISR and IMR)
// plus the PIO pins. Inside: PIO tile, DMA engine and rings, trace tap, event FIFO.
// @impl [SPEC-ICN-11] [SPEC-AXI-03] [SPEC-TRC-03] docs/INTERCONNECT_SPECIFICATION.md
module pio2_core #(
    parameter int RING_WORDS       = 256,
    parameter int TRACE_RING_WORDS = 512,
    parameter int FRAME_WORDS      = 28,
    parameter int TRACE_DEPTH      = 512
) (
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

    input  wire  [31:0]  s_axis_tdata,
    input  wire          s_axis_tvalid,
    output logic         s_axis_tready,
    input  wire          s_axis_tlast,
    input  wire  [3:0]   s_axis_tdest,

    output logic [31:0]  m_axis_tdata,
    output logic         m_axis_tvalid,
    input  wire          m_axis_tready,
    output logic         m_axis_tlast,
    output logic [3:0]   m_axis_tdest,

    output logic         o_irq,

    input  wire  [31:0]  i_gpio,
    output logic [31:0]  o_gpio_out,
    output logic [31:0]  o_gpio_pindir
);
    logic [31:0] timestamp;
    always_ff @(posedge clk or negedge rst_n)
        if (!rst_n) timestamp <= '0;
        else        timestamp <= timestamp + 32'd1;

    // ---------------------------------------------------------------- crossbar
    localparam int M = 3;               // 0 tile, 1 DMA, 2 trace + core registers
    logic [M*32-1:0] x_awaddr, x_wdata, x_araddr, x_rdata;
    logic [M*3-1:0]  x_awprot, x_arprot;
    logic [M*4-1:0]  x_wstrb;
    logic [M*2-1:0]  x_bresp, x_rresp;
    logic [M-1:0]    x_awvalid, x_awready, x_wvalid, x_wready, x_bvalid, x_bready;
    logic [M-1:0]    x_arvalid, x_arready, x_rvalid, x_rready;

    axil_crossbar #(
        .S_COUNT(1), .M_COUNT(M), .DATA_WIDTH(32), .ADDR_WIDTH(32),
        .M_BASE_ADDR({32'h0000_2000, 32'h0000_1000, 32'h0000_0000}),
        .M_ADDR_WIDTH({32'd13, 32'd12, 32'd12})
    ) u_xbar (
        .clk(clk), .rst(!rst_n),
        .s_axil_awaddr(s_axil_awaddr), .s_axil_awprot(3'b000), .s_axil_awvalid(s_axil_awvalid),
        .s_axil_awready(s_axil_awready), .s_axil_wdata(s_axil_wdata), .s_axil_wstrb(s_axil_wstrb),
        .s_axil_wvalid(s_axil_wvalid), .s_axil_wready(s_axil_wready), .s_axil_bresp(s_axil_bresp),
        .s_axil_bvalid(s_axil_bvalid), .s_axil_bready(s_axil_bready),
        .s_axil_araddr(s_axil_araddr), .s_axil_arprot(3'b000), .s_axil_arvalid(s_axil_arvalid),
        .s_axil_arready(s_axil_arready), .s_axil_rdata(s_axil_rdata), .s_axil_rresp(s_axil_rresp),
        .s_axil_rvalid(s_axil_rvalid), .s_axil_rready(s_axil_rready),
        .m_axil_awaddr(x_awaddr), .m_axil_awprot(x_awprot), .m_axil_awvalid(x_awvalid),
        .m_axil_awready(x_awready), .m_axil_wdata(x_wdata), .m_axil_wstrb(x_wstrb),
        .m_axil_wvalid(x_wvalid), .m_axil_wready(x_wready), .m_axil_bresp(x_bresp),
        .m_axil_bvalid(x_bvalid), .m_axil_bready(x_bready),
        .m_axil_araddr(x_araddr), .m_axil_arprot(x_arprot), .m_axil_arvalid(x_arvalid),
        .m_axil_arready(x_arready), .m_axil_rdata(x_rdata), .m_axil_rresp(x_rresp),
        .m_axil_rvalid(x_rvalid), .m_axil_rready(x_rready)
    );

    // ---------------------------------------------------------------- PIO tile
    logic [127:0] tx_tdata, rx_tdata;
    logic [3:0]   tx_tvalid, tx_tready, rx_tvalid, rx_tready;
    logic [19:0]  obs_pc;
    logic [3:0]   obs_stalled, obs_tx_empty, obs_rx_full, obs_active;
    logic [7:0]   obs_irq;
    logic         pio_irq;

    asp_pio_tile #(.BASE_ADDR(32'h0000_0000)) u_tile (
        .clk(clk), .rst_n(rst_n),
        .i_gpio(i_gpio), .o_gpio_out(o_gpio_out), .o_gpio_pindir(o_gpio_pindir),
        .s_axil_awaddr(x_awaddr[0 +: 32]), .s_axil_awvalid(x_awvalid[0]), .s_axil_awready(x_awready[0]),
        .s_axil_wdata(x_wdata[0 +: 32]), .s_axil_wstrb(x_wstrb[0 +: 4]), .s_axil_wvalid(x_wvalid[0]),
        .s_axil_wready(x_wready[0]), .s_axil_bresp(x_bresp[0 +: 2]), .s_axil_bvalid(x_bvalid[0]),
        .s_axil_bready(x_bready[0]), .s_axil_araddr(x_araddr[0 +: 32]), .s_axil_arvalid(x_arvalid[0]),
        .s_axil_arready(x_arready[0]), .s_axil_rdata(x_rdata[0 +: 32]), .s_axil_rresp(x_rresp[0 +: 2]),
        .s_axil_rvalid(x_rvalid[0]), .s_axil_rready(x_rready[0]),
        .s_axis_tx_tdata(tx_tdata), .s_axis_tx_tvalid(tx_tvalid), .s_axis_tx_tready(tx_tready),
        .m_axis_rx_tdata(rx_tdata), .m_axis_rx_tvalid(rx_tvalid), .m_axis_rx_tready(rx_tready),
        .o_pio_irq(pio_irq),
        .o_obs_pc(obs_pc), .o_obs_stalled(obs_stalled), .o_obs_tx_empty(obs_tx_empty),
        .o_obs_rx_full(obs_rx_full), .o_obs_active(obs_active), .o_obs_irq(obs_irq)
    );

    // ---------------------------------------------------------------- trace sources and event FIFO
    logic [4:0]  tap_valid;
    logic [39:0] tap_type, tap_src;
    logic [79:0] tap_data;
    logic [31:0] tap_merged;

    pio2_trace_tap u_tap (
        .clk(clk), .rst_n(rst_n),
        .i_obs_pc(obs_pc), .i_obs_stalled(obs_stalled), .i_obs_tx_empty(obs_tx_empty),
        .i_obs_rx_full(obs_rx_full), .i_obs_irq(obs_irq),
        .o_ev_valid(tap_valid), .o_ev_type(tap_type), .o_ev_src(tap_src), .o_ev_data(tap_data),
        .o_merged(tap_merged)
    );

    logic [7:0]   dma_ev_valid, dma_ev_merged;
    logic [63:0]  dma_ev_type, dma_ev_src;
    logic [127:0] dma_ev_data;
    logic [31:0]  trace_mask, trace_dropped;
    logic [$clog2(TRACE_DEPTH):0] trace_level;
    logic [63:0]  rec_tdata;
    logic         rec_tvalid, rec_tready;

    // Producers 0..7: DMA channels 2..9 (highest priority), 8..12: the trace tap ([SPEC-TRC-01])
    pio2_event_fifo #(.N_PROD(13), .DEPTH(TRACE_DEPTH)) u_events (
        .clk(clk), .rst_n(rst_n),
        .i_timestamp(timestamp), .i_type_mask(trace_mask),
        .ev_valid({tap_valid, dma_ev_valid}), .ev_type({tap_type, dma_ev_type}),
        .ev_src({tap_src, dma_ev_src}), .ev_data({tap_data, dma_ev_data}),
        .m_axis_tdata(rec_tdata), .m_axis_tvalid(rec_tvalid), .m_axis_tready(rec_tready),
        .o_level(trace_level), .o_dropped_total(trace_dropped)
    );

    logic [31:0] dma_merged;
    always_ff @(posedge clk or negedge rst_n)
        if (!rst_n)              dma_merged <= '0;
        else if (|dma_ev_merged) dma_merged <= dma_merged + 32'(4'($countones(dma_ev_merged)));

    // ---------------------------------------------------------------- DMA engine
    logic [8:0] dma_done, dma_err, stream_pend, done_flags;
    logic       in_drop;

    pio2_dma #(
        .RING_WORDS(RING_WORDS), .TRACE_RING_WORDS(TRACE_RING_WORDS), .FRAME_WORDS(FRAME_WORDS)
    ) u_dma (
        .clk(clk), .rst_n(rst_n),
        .s_axil_awaddr(x_awaddr[32 +: 32]), .s_axil_awvalid(x_awvalid[1]), .s_axil_awready(x_awready[1]),
        .s_axil_wdata(x_wdata[32 +: 32]), .s_axil_wstrb(x_wstrb[4 +: 4]), .s_axil_wvalid(x_wvalid[1]),
        .s_axil_wready(x_wready[1]), .s_axil_bresp(x_bresp[2 +: 2]), .s_axil_bvalid(x_bvalid[1]),
        .s_axil_bready(x_bready[1]), .s_axil_araddr(x_araddr[32 +: 32]), .s_axil_arvalid(x_arvalid[1]),
        .s_axil_arready(x_arready[1]), .s_axil_rdata(x_rdata[32 +: 32]), .s_axil_rresp(x_rresp[2 +: 2]),
        .s_axil_rvalid(x_rvalid[1]), .s_axil_rready(x_rready[1]),
        .m_tx_tdata(tx_tdata), .m_tx_tvalid(tx_tvalid), .m_tx_tready(tx_tready),
        .s_rx_tdata(rx_tdata), .s_rx_tvalid(rx_tvalid), .s_rx_tready(rx_tready),
        .i_tx_empty(obs_tx_empty), .i_irq(obs_irq), .i_gpio(i_gpio),
        .s_trace_tdata(rec_tdata), .s_trace_tvalid(rec_tvalid), .s_trace_tready(rec_tready),
        .s_axis_tdata(s_axis_tdata), .s_axis_tvalid(s_axis_tvalid), .s_axis_tready(s_axis_tready),
        .s_axis_tlast(s_axis_tlast), .s_axis_tdest(s_axis_tdest),
        .m_axis_tdata(m_axis_tdata), .m_axis_tvalid(m_axis_tvalid), .m_axis_tready(m_axis_tready),
        .m_axis_tlast(m_axis_tlast), .m_axis_tdest(m_axis_tdest),
        .i_done_flags(done_flags), .o_done(dma_done), .o_err(dma_err), .o_pend(stream_pend),
        .o_in_drop(in_drop),
        .o_ev_valid(dma_ev_valid), .o_ev_type(dma_ev_type), .o_ev_src(dma_ev_src),
        .o_ev_data(dma_ev_data), .o_ev_merged(dma_ev_merged)
    );

    // ---------------------------------------------------------------- trace and core registers
    pio2_core_regs u_core_regs (
        .clk(clk), .rst_n(rst_n),
        .s_axil_awaddr(x_awaddr[64 +: 32]), .s_axil_awvalid(x_awvalid[2]), .s_axil_awready(x_awready[2]),
        .s_axil_wdata(x_wdata[64 +: 32]), .s_axil_wstrb(x_wstrb[8 +: 4]), .s_axil_wvalid(x_wvalid[2]),
        .s_axil_wready(x_wready[2]), .s_axil_bresp(x_bresp[4 +: 2]), .s_axil_bvalid(x_bvalid[2]),
        .s_axil_bready(x_bready[2]), .s_axil_araddr(x_araddr[64 +: 32]), .s_axil_arvalid(x_arvalid[2]),
        .s_axil_arready(x_arready[2]), .s_axil_rdata(x_rdata[64 +: 32]), .s_axil_rresp(x_rresp[4 +: 2]),
        .s_axil_rvalid(x_rvalid[2]), .s_axil_rready(x_rready[2]),
        .o_trace_mask(trace_mask), .i_trace_level(16'(trace_level)), .i_trace_dropped(trace_dropped),
        .i_trace_merged(tap_merged + dma_merged), .i_timestamp(timestamp),
        .i_dma_done(dma_done), .i_dma_err(dma_err), .i_stream_pend(stream_pend), .i_in_drop(in_drop),
        .o_done_flags(done_flags),
        .i_pio_irq(pio_irq), .o_irq(o_irq)
    );

    /* verilator lint_off UNUSEDSIGNAL */
    wire _unused = &{1'b0, x_awprot, x_arprot, obs_active};
    /* verilator lint_on UNUSEDSIGNAL */
endmodule

`default_nettype wire
