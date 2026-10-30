// Copyright (C) 2026 Tim Michals
// SPDX-License-Identifier: GPL-3.0-or-later

`default_nettype none

// Event FIFO: producers offer records and never wait; one holding slot each, fixed priority
// (producer 0 highest), drop-and-count with a DROPPED record when the slots are idle. Storage is Forencich axis_fifo.
// @impl [SPEC-TRC-01] docs/INTERCONNECT_SPECIFICATION.md
module pio2_event_fifo #(
    parameter int N_PROD = 4,
    parameter int DEPTH  = 512
) (
    input  wire                     clk,
    input  wire                     rst_n,

    input  wire  [31:0]             i_timestamp,
    input  wire  [31:0]             i_type_mask,     // bit n enables type code n (0x00..0x1F)

    input  wire  [N_PROD-1:0]       ev_valid,
    input  wire  [N_PROD*8-1:0]     ev_type,
    input  wire  [N_PROD*8-1:0]     ev_src,
    input  wire  [N_PROD*16-1:0]    ev_data,

    output wire  [63:0]             m_axis_tdata,    // [15:0] data [23:16] src [31:24] type [63:32] ts
    output wire                     m_axis_tvalid,
    input  wire                     m_axis_tready,

    output wire  [$clog2(DEPTH):0]  o_level,
    output logic [31:0]             o_dropped_total
);
    localparam logic [7:0] T_DROPPED = 8'h1F;
    localparam int CW = $clog2(N_PROD + 1);

    logic [N_PROD-1:0] slot_full;
    logic [63:0]       slot_rec [N_PROD];

    // Output stage into the FIFO: loaded only when empty or taken, so TVALID / TDATA stay stable
    // until TREADY ([SPEC-AXI-02]).
    logic        out_valid;
    logic [63:0] out_data;
    wire         out_ready;
    wire         load = !out_valid || out_ready;

    logic [15:0] drop_cnt;          // records lost since the last DROPPED record (saturating)
    logic [31:0] drop_ts;           // clock of the first of them

    logic [N_PROD-1:0] en, take, drop;
    logic              take_drop;
    logic [CW-1:0]     ndrop;

    always_comb begin
        take      = '0;
        take_drop = 1'b0;
        if (load) begin
            for (int i = N_PROD - 1; i >= 0; i--) begin
                if (slot_full[i]) begin
                    take    = '0;
                    take[i] = 1'b1;
                end
            end
            take_drop = (slot_full == '0) && (drop_cnt != 16'd0);   // lowest priority: never adds drops
        end
        ndrop = '0;
        for (int i = 0; i < N_PROD; i++) begin
            en[i]   = ev_valid[i] && (ev_type[i*8+5 +: 3] == 3'd0) && i_type_mask[ev_type[i*8 +: 5]];
            drop[i] = en[i] && slot_full[i] && !take[i];
            ndrop   = ndrop + CW'(drop[i]);
        end
    end

    function automatic logic [15:0] sat16(input logic [16:0] v);
        return v[16] ? 16'hFFFF : v[15:0];
    endfunction

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            slot_full       <= '0;
            for (int i = 0; i < N_PROD; i++) slot_rec[i] <= '0;
            out_valid       <= 1'b0;
            out_data        <= '0;
            drop_cnt        <= '0;
            drop_ts         <= '0;
            o_dropped_total <= '0;
        end else begin
            for (int i = 0; i < N_PROD; i++) begin
                if (take[i])
                    slot_full[i] <= 1'b0;
                if (en[i] && !drop[i]) begin
                    slot_full[i] <= 1'b1;
                    slot_rec[i]  <= {i_timestamp, ev_type[i*8 +: 8], ev_src[i*8 +: 8], ev_data[i*16 +: 16]};
                end
            end

            if (load) begin
                out_valid <= take_drop || (|take);
                if (take_drop)
                    out_data <= {drop_ts, T_DROPPED, 8'h00, drop_cnt};
                for (int i = 0; i < N_PROD; i++)
                    if (take[i]) out_data <= slot_rec[i];
            end

            if (take_drop) begin
                drop_cnt <= 16'(ndrop);
                drop_ts  <= i_timestamp;
            end else if (ndrop != '0) begin
                if (drop_cnt == 16'd0)
                    drop_ts <= i_timestamp;
                drop_cnt <= sat16({1'b0, drop_cnt} + 17'(ndrop));
            end

            if (ndrop != '0)
                o_dropped_total <= ({1'b0, o_dropped_total} + 33'(ndrop) > 33'hFFFF_FFFF)
                                   ? 32'hFFFF_FFFF : o_dropped_total + 32'(ndrop);
        end
    end

    axis_fifo #(
        .DEPTH       (DEPTH),
        .DATA_WIDTH  (64),
        .KEEP_ENABLE (0),
        .LAST_ENABLE (0),
        .ID_ENABLE   (0),
        .DEST_ENABLE (0),
        .USER_ENABLE (0),
        .FRAME_FIFO  (0)
    ) u_fifo (
        .clk                 (clk),
        .rst                 (!rst_n),
        .s_axis_tdata        (out_data),
        .s_axis_tkeep        (8'hFF),
        .s_axis_tvalid       (out_valid),
        .s_axis_tready       (out_ready),
        .s_axis_tlast        (1'b0),
        .s_axis_tid          (8'd0),
        .s_axis_tdest        (8'd0),
        .s_axis_tuser        (1'b0),
        .m_axis_tdata        (m_axis_tdata),
        .m_axis_tkeep        (),
        .m_axis_tvalid       (m_axis_tvalid),
        .m_axis_tready       (m_axis_tready),
        .m_axis_tlast        (),
        .m_axis_tid          (),
        .m_axis_tdest        (),
        .m_axis_tuser        (),
        .pause_req           (1'b0),
        .pause_ack           (),
        .status_depth        (o_level),
        .status_depth_commit (),
        .status_overflow     (),
        .status_bad_frame    (),
        .status_good_frame   ()
    );
endmodule

`default_nettype wire
