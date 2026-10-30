// Copyright (C) 2026 Tim Michals
// SPDX-License-Identifier: GPL-3.0-or-later

`default_nettype none

// Trace tap: edges on the tile's observation port become event records, one producer per SM and
// one for the IRQ flags. Several edges of one producer in one clock leave one per clock.
// @impl [SPEC-TRC-03] docs/INTERCONNECT_SPECIFICATION.md
module pio2_trace_tap (
    input  wire          clk,
    input  wire          rst_n,

    input  wire  [19:0]  i_obs_pc,
    input  wire  [3:0]   i_obs_stalled,
    input  wire  [3:0]   i_obs_tx_empty,
    input  wire  [3:0]   i_obs_rx_full,
    input  wire  [7:0]   i_obs_irq,

    // producer 0 = IRQ flags, producers 1..4 = SM0..SM3
    output logic [4:0]   o_ev_valid,
    output logic [39:0]  o_ev_type,
    output logic [39:0]  o_ev_src,
    output logic [79:0]  o_ev_data,
    output logic [31:0]  o_merged
);
    localparam logic [7:0] T_STALL_BEGIN = 8'h01, T_STALL_END = 8'h02, T_IRQ_SET = 8'h03,
                           T_IRQ_CLR = 8'h04, T_FIFO_FULL = 8'h05, T_FIFO_EMPTY = 8'h06;

    logic [3:0] stalled_q, tx_empty_q, rx_full_q;
    logic [7:0] irq_q;

    // pending kinds per SM: [0] stall begin [1] stall end [2] RX full [3] TX empty
    logic [3:0] pend [4];
    logic [4:0] pc_begin [4];          // PC of the stalled instruction, for both stall records
    logic [7:0] irq_set_p, irq_clr_p;

    function automatic logic [2:0] first8(input logic [7:0] m);
        first8 = 3'd0;
        for (int i = 7; i >= 0; i--) if (m[i]) first8 = 3'(i);
    endfunction


    always_comb begin
        o_ev_valid = '0;
        o_ev_type  = '0;
        o_ev_src   = '0;
        o_ev_data  = '0;
        if (irq_set_p != 8'd0) begin
            o_ev_valid[0]     = 1'b1;
            o_ev_type[7:0]    = T_IRQ_SET;
            o_ev_src[7:0]     = 8'hFF;
            o_ev_data[15:0]   = {13'd0, first8(irq_set_p)};
        end else if (irq_clr_p != 8'd0) begin
            o_ev_valid[0]     = 1'b1;
            o_ev_type[7:0]    = T_IRQ_CLR;
            o_ev_src[7:0]     = 8'hFF;
            o_ev_data[15:0]   = {13'd0, first8(irq_clr_p)};
        end
        for (int n = 0; n < 4; n++) begin
            if (pend[n] != 4'd0) begin
                o_ev_valid[n + 1]          = 1'b1;
                o_ev_src[8*(n+1) +: 8]     = 8'(n);
                if (pend[n][0]) begin
                    o_ev_type[8*(n+1) +: 8]  = T_STALL_BEGIN;
                    o_ev_data[16*(n+1) +: 16] = {11'd0, pc_begin[n]};
                end else if (pend[n][1]) begin
                    o_ev_type[8*(n+1) +: 8]  = T_STALL_END;
                    o_ev_data[16*(n+1) +: 16] = {11'd0, pc_begin[n]};
                end else if (pend[n][2]) begin
                    o_ev_type[8*(n+1) +: 8]  = T_FIFO_FULL;
                    o_ev_data[16*(n+1) +: 16] = 16'd1;
                end else begin
                    o_ev_type[8*(n+1) +: 8]  = T_FIFO_EMPTY;
                    o_ev_data[16*(n+1) +: 16] = 16'd0;
                end
            end
        end
    end

    // next state: the offered record leaves its pending bit, new edges join
    logic [7:0] irq_set_n, irq_clr_n;
    logic [3:0] pend_n [4];
    logic [5:0] merges;
    always_comb begin
        logic [7:0] rise, fall;
        logic [3:0] pp, e;
        irq_set_n = irq_set_p;
        irq_clr_n = irq_clr_p;
        if (irq_set_n != 8'd0)      irq_set_n[first8(irq_set_n)] = 1'b0;
        else if (irq_clr_n != 8'd0) irq_clr_n[first8(irq_clr_n)] = 1'b0;
        rise = i_obs_irq & ~irq_q;
        fall = ~i_obs_irq & irq_q;
        merges = 6'($countones(rise & irq_set_n)) + 6'($countones(fall & irq_clr_n));
        irq_set_n = irq_set_n | rise;
        irq_clr_n = irq_clr_n | fall;
        for (int n = 0; n < 4; n++) begin
            pp = pend[n];
            if (pp[0])      pp[0] = 1'b0;
            else if (pp[1]) pp[1] = 1'b0;
            else if (pp[2]) pp[2] = 1'b0;
            else if (pp[3]) pp[3] = 1'b0;
            e = {i_obs_tx_empty[n] & ~tx_empty_q[n], i_obs_rx_full[n] & ~rx_full_q[n],
                 ~i_obs_stalled[n] & stalled_q[n], i_obs_stalled[n] & ~stalled_q[n]};
            merges = merges + 6'($countones(e & pp));
            pend_n[n] = pp | e;
        end
    end

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            stalled_q  <= '0;
            tx_empty_q <= 4'hF;
            rx_full_q  <= '0;
            irq_q      <= '0;
            irq_set_p  <= '0;
            irq_clr_p  <= '0;
            o_merged   <= '0;
            for (int n = 0; n < 4; n++) begin
                pend[n]     <= '0;
                pc_begin[n] <= '0;
            end
        end else begin
            stalled_q  <= i_obs_stalled;
            tx_empty_q <= i_obs_tx_empty;
            rx_full_q  <= i_obs_rx_full;
            irq_q      <= i_obs_irq;
            irq_set_p  <= irq_set_n;
            irq_clr_p  <= irq_clr_n;
            for (int n = 0; n < 4; n++) begin
                pend[n] <= pend_n[n];
                if (i_obs_stalled[n] & ~stalled_q[n]) pc_begin[n] <= i_obs_pc[5*n +: 5];
            end
            if (merges != 6'd0)
                o_merged <= (o_merged == 32'hFFFF_FFFF) ? o_merged : o_merged + 32'(merges);
        end
    end
endmodule

`default_nettype wire
