// Copyright (C) 2026 Tim Michals
// SPDX-License-Identifier: GPL-3.0-or-later

`default_nettype none

// One DMA channel: a ring (Forencich axis_fifo) between the link side and one PIO FIFO, the
// mode logic that paces the PIO side, the out-frame packetizer, counters and trace events.
//   IN  (i_dir = 1): link stream -> ring -> PIO TX FIFO
//   OUT (i_dir = 0): PIO RX FIFO -> ring -> link stream, in blocks of at most FRAME_WORDS words
// The occupancy counter counts words between the ring's two ports exactly (the FIFO's own depth
// output leaves out the words in its output stage), and the ring takes at most RING_WORDS words.
// @impl [SPEC-DMA-01] [SPEC-DMA-02] docs/INTERCONNECT_SPECIFICATION.md
module pio2_dma_chan #(
    parameter int RING_WORDS  = 256,
    parameter int FRAME_WORDS = 28
) (
    input  wire          clk,
    input  wire          rst_n,

    // configuration
    input  wire          i_en,
    input  wire          i_dir,          // 1 = IN, 0 = OUT
    input  wire  [1:0]   i_mode,         // 0 ONE_SHOT, 1 RING, 2 TRIGGERED, 3 DIRECT
    input  wire  [15:0]  i_count,        // N (0 = 65536)
    input  wire  [23:0]  i_flush,        // clocks a partial out frame may wait (0 = whole frames)
    input  wire  [15:0]  i_thr,
    input  wire          i_start,        // pulses
    input  wire          i_reset_ring,
    input  wire          i_trigger,

    // link side
    input  wire  [31:0]  s_in_tdata,
    input  wire          s_in_tvalid,
    output logic         s_in_tready,
    output logic [31:0]  m_out_tdata,
    output logic         m_out_tvalid,
    input  wire          m_out_tready,
    output logic         m_out_tlast,

    // PIO side
    output logic [31:0]  m_tx_tdata,
    output logic         m_tx_tvalid,
    input  wire          m_tx_tready,
    input  wire          i_tx_empty,
    input  wire  [31:0]  s_rx_tdata,
    input  wire          s_rx_tvalid,
    output logic         s_rx_tready,

    // status and counters
    output logic [15:0]  o_level,
    output logic [15:0]  o_free,
    output logic         o_busy,
    output logic [15:0]  o_remaining,
    output logic         o_pend,
    output logic         o_done,         // pulses
    output logic         o_err,
    output logic         o_in_drop,
    output logic [31:0]  o_moved,
    output logic [31:0]  o_underruns,
    output logic [31:0]  o_overruns,
    output logic [31:0]  o_triggers,
    input  wire          i_clr_moved,
    input  wire          i_clr_underruns,
    input  wire          i_clr_overruns,
    input  wire          i_clr_triggers,

    // trace producer ([SPEC-TRC-01]); src is set by the parent
    output logic         o_ev_valid,
    output logic [7:0]   o_ev_type,
    output logic [15:0]  o_ev_data,
    output logic         o_ev_merged
);
    localparam logic [1:0] M_ONE_SHOT = 2'd0, M_RING = 2'd1, M_TRIGGERED = 2'd2, M_DIRECT = 2'd3;
    localparam int CW = $clog2(RING_WORDS + 1);
    localparam int FW = $clog2(FRAME_WORDS + 1);

    wire        is_in   = i_dir;
    wire        counted = i_mode == M_ONE_SHOT || i_mode == M_TRIGGERED;
    wire [16:0] n_words = i_count == 16'd0 ? 17'h10000 : {1'b0, i_count};

    logic [16:0] remaining;
    wire allowed = i_en && (!counted || remaining != 17'd0);

    // ---------------------------------------------------------------- ring
    logic [CW-1:0] cnt;
    logic          ring_rst, reset_pending, sending;
    wire           room = cnt < CW'(RING_WORDS);

    logic [31:0] fifo_s_tdata, fifo_m_tdata;
    logic        fifo_s_tvalid, fifo_s_tready, fifo_m_tvalid, fifo_m_tready;

    assign fifo_s_tdata  = is_in ? s_in_tdata : s_rx_tdata;
    assign fifo_s_tvalid = room && (is_in ? (i_en && s_in_tvalid) : (allowed && s_rx_tvalid));
    wire   ring_ready    = room && fifo_s_tready;

    assign s_in_tready = (i_en && is_in) ? ring_ready : 1'b1;          // not an IN channel: dropped
    assign o_in_drop   = s_in_tvalid && !(i_en && is_in);
    assign s_rx_tready = !is_in && allowed && ring_ready;

    assign m_tx_tdata  = fifo_m_tdata;
    assign m_tx_tvalid = is_in && allowed && fifo_m_tvalid;

    assign m_out_tdata  = fifo_m_tdata;
    assign m_out_tvalid = !is_in && sending && fifo_m_tvalid;
    assign fifo_m_tready = is_in ? (allowed && m_tx_tready) : (sending && m_out_tready);

    wire ring_push = fifo_s_tvalid && fifo_s_tready;
    wire ring_pop  = fifo_m_tvalid && fifo_m_tready;
    wire pio_move  = is_in ? (m_tx_tvalid && m_tx_tready) : (s_rx_tvalid && s_rx_tready);

    axis_fifo #(
        .DEPTH(RING_WORDS), .DATA_WIDTH(32), .KEEP_ENABLE(0), .LAST_ENABLE(0), .ID_ENABLE(0),
        .DEST_ENABLE(0), .USER_ENABLE(0), .RAM_PIPELINE(1), .FRAME_FIFO(0)
    ) u_ring (
        .clk(clk), .rst(!rst_n || ring_rst),
        .s_axis_tdata(fifo_s_tdata), .s_axis_tkeep(4'hF), .s_axis_tvalid(fifo_s_tvalid),
        .s_axis_tready(fifo_s_tready), .s_axis_tlast(1'b0), .s_axis_tid(8'd0), .s_axis_tdest(8'd0),
        .s_axis_tuser(1'b0),
        .m_axis_tdata(fifo_m_tdata), .m_axis_tkeep(), .m_axis_tvalid(fifo_m_tvalid),
        .m_axis_tready(fifo_m_tready), .m_axis_tlast(), .m_axis_tid(), .m_axis_tdest(), .m_axis_tuser(),
        .pause_req(1'b0), .pause_ack(),
        .status_depth(), .status_depth_commit(), .status_overflow(), .status_bad_frame(),
        .status_good_frame()
    );

    // ---------------------------------------------------------------- out packetizer
    logic [FW-1:0] left;
    logic [23:0]   wait_clk;
    wire  [FW-1:0] frame_len = (i_mode == M_DIRECT) ? FW'(1)
                             : (cnt >= CW'(FRAME_WORDS) ? FW'(FRAME_WORDS) : FW'(cnt));
    wire start_frame = !is_in && !sending && !reset_pending && cnt != '0 &&
                       (i_mode == M_DIRECT || cnt >= CW'(FRAME_WORDS) || !room ||
                        (i_flush != 24'd0 && wait_clk >= i_flush));
    assign m_out_tlast = left == FW'(1);

    // ---------------------------------------------------------------- counters, flags, events
    logic en_q, primed, blk_active, under_q, over_q;
    wire  under_cond = i_en && is_in && primed && (!counted || remaining != 17'd0) &&
                       i_tx_empty && cnt == '0;
    wire  over_cond  = i_en && !is_in && allowed && s_rx_tvalid && !ring_ready;
    wire  under_start = under_cond && !under_q;
    wire  over_start  = over_cond && !over_q;
    wire  trig        = i_en && i_mode == M_TRIGGERED && i_trigger;
    wire  blk_begin   = counted && pio_move && !blk_active;
    wire  blk_done    = counted && pio_move && remaining == 17'd1 && !trig;

    function automatic logic [31:0] sat_inc(input logic [31:0] v);
        return &v ? v : v + 32'd1;
    endfunction

    // events: 0 trigger, 1 begin, 2 done, 3 underrun, 4 overrun (offered in this order)
    logic [4:0]  pend;
    logic [15:0] ev_d [5];
    wire  [4:0]  new_ev = {over_start, under_start, blk_done, blk_begin, trig};
    logic [4:0]  offer;
    always_comb begin
        offer = '0;
        for (int i = 4; i >= 0; i--) if (pend[i]) begin offer = '0; offer[i] = 1'b1; end
    end
    assign o_ev_valid = pend != '0;
    always_comb begin
        o_ev_type = 8'h00;
        o_ev_data = 16'd0;
        for (int i = 0; i < 5; i++) if (offer[i]) begin
            o_ev_type = (i == 0) ? 8'h0C : (i == 1) ? 8'h08 : (i == 2) ? 8'h09 : (i == 3) ? 8'h0A : 8'h0B;
            o_ev_data = ev_d[i];
        end
    end

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            remaining <= '0; cnt <= '0; ring_rst <= 1'b0; reset_pending <= 1'b0;
            sending <= 1'b0; left <= '0; wait_clk <= '0;
            en_q <= 1'b0; primed <= 1'b0; blk_active <= 1'b0; under_q <= 1'b0; over_q <= 1'b0;
            o_moved <= '0; o_underruns <= '0; o_overruns <= '0; o_triggers <= '0;
            pend <= '0; o_ev_merged <= 1'b0;
            for (int i = 0; i < 5; i++) ev_d[i] <= '0;
        end else begin
            en_q    <= i_en;
            under_q <= under_cond;
            over_q  <= over_cond;

            // remaining words of the current ONE_SHOT / TRIGGERED work
            if (!i_en) begin
                remaining <= '0;
            end else if (i_mode == M_ONE_SHOT && ((i_en && !en_q) || i_start)) begin
                remaining <= n_words;
            end else if (trig) begin
                remaining <= remaining + n_words - 17'(pio_move && counted) > remaining
                           ? remaining + n_words - 17'(pio_move && counted) : '1;
            end else if (counted && pio_move) begin
                remaining <= remaining - 17'd1;
            end

            if (!i_en || i_start || (i_en && !en_q)) primed <= 1'b0;
            else if (pio_move)                       primed <= 1'b1;
            if (!i_en)          blk_active <= 1'b0;
            else if (blk_done)  blk_active <= 1'b0;
            else if (blk_begin) blk_active <= 1'b1;

            // ring occupancy; RESET_RING waits for an out frame being sent to finish
            ring_rst <= 1'b0;
            if (i_reset_ring) reset_pending <= 1'b1;
            if (reset_pending && !sending && !ring_rst) begin
                ring_rst <= 1'b1;
                reset_pending <= 1'b0;
            end
            if (ring_rst) cnt <= '0;
            else          cnt <= cnt + CW'(ring_push) - CW'(ring_pop);

            // packetizer
            if (sending) begin
                if (ring_pop) begin
                    left <= left - FW'(1);
                    if (left == FW'(1)) sending <= 1'b0;
                end
            end else if (start_frame) begin
                sending <= 1'b1;
                left <= frame_len;
            end
            if (sending || cnt == '0)    wait_clk <= '0;
            else if (wait_clk != '1)     wait_clk <= wait_clk + 24'd1;

            // counters
            if (i_clr_moved)          o_moved <= '0;
            else if (pio_move)        o_moved <= o_moved + 32'd1;
            if (i_clr_underruns)      o_underruns <= '0;
            else if (under_start)     o_underruns <= sat_inc(o_underruns);
            if (i_clr_overruns)       o_overruns <= '0;
            else if (over_start)      o_overruns <= sat_inc(o_overruns);
            if (i_clr_triggers)       o_triggers <= '0;
            else if (trig)            o_triggers <= sat_inc(o_triggers);

            // events: the offered one leaves; a new one while the same type waits is merged
            o_ev_merged <= |(new_ev & pend & ~offer);
            pend <= (pend & ~offer) | new_ev;
            if (trig)        ev_d[0] <= 16'(sat_inc(o_triggers));
            if (blk_begin)   ev_d[1] <= n_words[15:0];
            if (blk_done)    ev_d[2] <= n_words[15:0];
            if (under_start) ev_d[3] <= 16'(sat_inc(o_underruns));
            if (over_start)  ev_d[4] <= 16'(sat_inc(o_overruns));
        end
    end

    assign o_level     = 16'(cnt);
    assign o_free      = 16'(CW'(RING_WORDS) - cnt);
    assign o_busy      = allowed && (counted || i_mode != M_ONE_SHOT);
    assign o_remaining = remaining[16] ? 16'hFFFF : remaining[15:0];
    assign o_pend      = i_en && i_thr != 16'd0 &&
                         (is_in ? o_free >= i_thr : o_level >= i_thr);
    assign o_done      = blk_done;
    assign o_err       = under_start || over_start;
endmodule

`default_nettype wire
