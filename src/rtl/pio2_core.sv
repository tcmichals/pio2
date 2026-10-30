// Copyright (C) 2026 Tim Michals
// SPDX-License-Identifier: GPL-3.0-or-later

`default_nettype none

// pio2 core, first part: PIO tile + trace tap + event FIFO. The AXI4-Lite port goes straight to the
// tile until the crossbar exists; the record stream is a port until the DMA trace channel exists.
// @impl [SPEC-TRC-03] [SPEC-ICN-11] docs/INTERCONNECT_SPECIFICATION.md
module pio2_core #(
    parameter int TRACE_DEPTH = 512
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

    input  wire  [31:0]  i_gpio,
    output logic [31:0]  o_gpio_out,
    output logic [31:0]  o_gpio_pindir,
    output logic         o_irq,

    input  wire  [31:0]  i_trace_mask,
    output logic [63:0]  m_trace_tdata,
    output logic         m_trace_tvalid,
    input  wire          m_trace_tready,
    output logic [31:0]  o_trace_dropped,
    output logic [31:0]  o_trace_merged
);
    logic [31:0] timestamp;
    always_ff @(posedge clk or negedge rst_n)
        if (!rst_n) timestamp <= '0;
        else        timestamp <= timestamp + 32'd1;

    logic [19:0] obs_pc;
    logic [3:0]  obs_stalled, obs_tx_empty, obs_rx_full;
    logic [7:0]  obs_irq;
    /* verilator lint_off UNUSEDSIGNAL */
    logic [3:0]   obs_active, tx_ready_unused, rx_valid_unused;
    logic [127:0] rx_data_unused;
    logic [9:0]   level_unused;
    /* verilator lint_on UNUSEDSIGNAL */

    asp_pio_tile #(.BASE_ADDR(32'h0000_0000)) u_tile (
        .clk (clk), .rst_n (rst_n),
        .i_gpio (i_gpio), .o_gpio_out (o_gpio_out), .o_gpio_pindir (o_gpio_pindir),
        .s_axil_awaddr (s_axil_awaddr), .s_axil_awvalid (s_axil_awvalid), .s_axil_awready (s_axil_awready),
        .s_axil_wdata (s_axil_wdata), .s_axil_wstrb (s_axil_wstrb), .s_axil_wvalid (s_axil_wvalid),
        .s_axil_wready (s_axil_wready), .s_axil_bresp (s_axil_bresp), .s_axil_bvalid (s_axil_bvalid),
        .s_axil_bready (s_axil_bready), .s_axil_araddr (s_axil_araddr), .s_axil_arvalid (s_axil_arvalid),
        .s_axil_arready (s_axil_arready), .s_axil_rdata (s_axil_rdata), .s_axil_rresp (s_axil_rresp),
        .s_axil_rvalid (s_axil_rvalid), .s_axil_rready (s_axil_rready),
        .s_axis_tx_tdata ('0), .s_axis_tx_tvalid (4'd0), .s_axis_tx_tready (tx_ready_unused),
        .m_axis_rx_tdata (rx_data_unused), .m_axis_rx_tvalid (rx_valid_unused), .m_axis_rx_tready (4'd0),
        .o_pio_irq (o_irq),
        .o_obs_pc (obs_pc), .o_obs_stalled (obs_stalled), .o_obs_tx_empty (obs_tx_empty),
        .o_obs_rx_full (obs_rx_full), .o_obs_active (obs_active), .o_obs_irq (obs_irq)
    );

    logic [4:0]  ev_valid;
    logic [39:0] ev_type, ev_src;
    logic [79:0] ev_data;

    pio2_trace_tap u_tap (
        .clk (clk), .rst_n (rst_n),
        .i_obs_pc (obs_pc), .i_obs_stalled (obs_stalled), .i_obs_tx_empty (obs_tx_empty),
        .i_obs_rx_full (obs_rx_full), .i_obs_irq (obs_irq),
        .o_ev_valid (ev_valid), .o_ev_type (ev_type), .o_ev_src (ev_src), .o_ev_data (ev_data),
        .o_merged (o_trace_merged)
    );

    pio2_event_fifo #(.N_PROD(5), .DEPTH(TRACE_DEPTH)) u_events (
        .clk (clk), .rst_n (rst_n),
        .i_timestamp (timestamp), .i_type_mask (i_trace_mask),
        .ev_valid (ev_valid), .ev_type (ev_type), .ev_src (ev_src), .ev_data (ev_data),
        .m_axis_tdata (m_trace_tdata), .m_axis_tvalid (m_trace_tvalid), .m_axis_tready (m_trace_tready),
        .o_level (level_unused), .o_dropped_total (o_trace_dropped)
    );
endmodule

`default_nettype wire
