// Copyright (C) 2026 Tim Michals
// SPDX-License-Identifier: GPL-3.0-or-later

`default_nettype none

// SPI PHY ([SPEC-LINK-01]): mode 0, single or dual data, chosen per window by the command byte.
//
// SCK clocks the shift logic directly. Bytes cross between SCK and the system clock in frame
// buffers, two per direction:
//   rx_buf[b]: written in the SCK domain during a WRITE_FRAME window, read by the system domain
//              after the window (CS_n high);
//   tx_buf[b]: written by the system domain while not offered, read in the SCK domain during a
//              READ_FRAME window.
// The system domain's offer (i_rx_*, i_tx_*, i_isr) changes only while CS_n is high and is latched
// at the window's first SCK edge. At each rising edge the window summary (command, bytes, latched
// offer) is updated; it holds still after CS_n rises, when the system domain reads it (o_win pulse).
// Synchronised signals: CS_n and the window toggle (2 flops each).
// @impl [SPEC-LINK-01] docs/INTERCONNECT_SPECIFICATION.md
module pio2_spi_phy (
    input  wire          clk,
    input  wire          rst_n,

    // pins (tristate in the board top)
    input  wire          sck,
    input  wire          cs_n,
    input  wire          io0_i,
    output logic         io0_o,
    output logic         io0_oe,
    input  wire          io1_i,
    output logic         io1_o,
    output logic         io1_oe,

    // offer from the system domain (changes only while o_cs_idle)
    input  wire  [7:0]   i_isr,
    input  wire          i_rx_ok,
    input  wire          i_rx_sel,
    input  wire          i_tx_valid,
    input  wire          i_tx_sel,
    input  wire  [7:0]   i_tx_len0,
    input  wire  [7:0]   i_tx_len1,

    // system-domain buffer ports
    input  wire          i_rx_rbuf,
    input  wire  [6:0]   i_rx_raddr,
    output logic [7:0]   o_rx_rdata,
    input  wire          i_tx_we,
    input  wire          i_tx_wbuf,
    input  wire  [6:0]   i_tx_waddr,
    input  wire  [7:0]   i_tx_wdata,
    input  wire  [6:0]   i_tx_raddr1,       // read back of tx buffer 1 (stream frames, for the CRC)
    output logic [7:0]   o_tx_rdata1,

    // window summary, valid in the clock of o_win
    output logic         o_cs_idle,         // CS_n high (synchronised) for 2 clocks
    output logic         o_win,
    output logic [7:0]   o_win_cmd,
    output logic [7:0]   o_win_count,       // data bytes written (WRITE) or fully sent (READ), max 255
    output logic         o_win_rx_ok,
    output logic         o_win_rx_sel,
    output logic         o_win_tx_valid,
    output logic         o_win_tx_sel
);
    localparam logic [6:0] OP_WRITE = 7'h01, OP_READ = 7'h02;

    logic [7:0] rx_buf0 [128];
    logic [7:0] rx_buf1 [128];
    logic [7:0] tx_buf0 [128];
    logic [7:0] tx_buf1 [128];

    // ---------------------------------------------------------------- SCK domain, rising edges
    logic [10:0] e;                      // rising edges so far in this window
    logic [6:0]  cmd_sr;
    logic [7:0]  cmd;                    // valid from edge 8
    logic [7:0]  sr;
    logic        lat_rx_ok, lat_rx_sel, lat_tx_valid, lat_tx_sel;
    logic [7:0]  lat_len;
    logic        win_tgl;
    logic [7:0]  sum_cmd, sum_count;

    wire dual    = cmd[7];
    wire is_wr   = cmd[6:0] == OP_WRITE;
    wire is_rd   = cmd[6:0] == OP_READ;
    // bit position of this rising edge inside the data phase
    wire [10:0] d  = e - 11'd8;          // single: bit d of the data
    wire [10:0] pr = e - 11'd9;          // dual read: pair after the turnaround clock
    wire [10:0] pw = e - 11'd8;          // dual write: pair
    wire        byte_done = dual ? (is_rd ? pr[1:0] == 2'd3 : pw[1:0] == 2'd3) : d[2:0] == 3'd7;
    wire [10:0] byte_idx  = dual ? (is_rd ? pr >> 2 : pw >> 2) : d >> 3;
    wire [7:0]  rx_byte   = dual ? {sr[5:0], io1_i, io0_i} : {sr[6:0], io0_i};
    wire        data_edge = e >= 11'd8 && !(dual && is_rd && e == 11'd8);

    always_ff @(posedge sck or posedge cs_n) begin
        if (cs_n) e <= '0;
        else if (e != '1) e <= e + 11'd1;
    end

    always_ff @(posedge sck) begin
        if (!cs_n) begin
            if (e == 11'd0) begin
                lat_rx_ok <= i_rx_ok; lat_rx_sel <= i_rx_sel;
                lat_tx_valid <= i_tx_valid; lat_tx_sel <= i_tx_sel;
                lat_len <= i_tx_sel ? i_tx_len1 : i_tx_len0;
                win_tgl <= !win_tgl;
                sum_cmd <= 8'd0; sum_count <= 8'd0;
            end
            if (e < 11'd8) cmd_sr <= {cmd_sr[5:0], io0_i};
            if (e == 11'd7) begin
                cmd <= {cmd_sr, io0_i};
                sum_cmd <= {cmd_sr, io0_i};
            end
            if (data_edge) begin
                sr <= dual ? {sr[5:0], io1_i, io0_i} : {sr[6:0], io0_i};
                if (byte_done && byte_idx < 11'd128) begin
                    if (is_wr && lat_rx_ok) begin
                        if (lat_rx_sel) rx_buf1[byte_idx[6:0]] <= rx_byte;
                        else            rx_buf0[byte_idx[6:0]] <= rx_byte;
                    end
                    if (is_wr || is_rd) sum_count <= 8'(byte_idx + 11'd1);
                end
            end
        end
    end

    // ---------------------------------------------------------------- SCK domain, falling edges
    // Outputs change after the falling edge; before the first one the ISR's bit 7 is out.
    logic       first, o1, o0, oe1, oe0;
    wire [10:0] next_d  = e - 11'd8;
    wire [10:0] next_p  = e - 11'd9;
    wire [10:0] nidx    = dual ? next_p >> 2 : next_d >> 3;
    wire [7:0]  tx_byte = (lat_tx_valid && nidx < {3'd0, lat_len})
                        ? (lat_tx_sel ? tx_buf1[nidx[6:0]] : tx_buf0[nidx[6:0]]) : 8'd0;
    always_ff @(negedge sck or posedge cs_n) begin
        if (cs_n) begin
            first <= 1'b1; o1 <= 1'b0; o0 <= 1'b0; oe1 <= 1'b1; oe0 <= 1'b0;
        end else begin
            first <= 1'b0;
            if (e < 11'd8) begin                         // command byte: the ISR goes out on IO1
                o1 <= i_isr[3'd7 - e[2:0]]; oe1 <= 1'b1; oe0 <= 1'b0;
            end else if (is_rd && dual) begin
                oe1 <= e >= 11'd9; oe0 <= e >= 11'd9;    // edge 8 is the turnaround clock
                o1 <= tx_byte[3'd7 - {next_p[1:0], 1'b0}];
                o0 <= tx_byte[3'd6 - {next_p[1:0], 1'b0}];
            end else if (is_rd) begin
                o1 <= tx_byte[3'd7 - next_d[2:0]]; oe1 <= 1'b1; oe0 <= 1'b0;
            end else begin
                o1 <= 1'b0; oe1 <= !dual; oe0 <= 1'b0;   // writes: zeros on MISO (single only)
            end
        end
    end
    assign io1_o  = first ? i_isr[7] : o1;
    assign io0_o  = o0;
    assign io1_oe = !cs_n && (first || oe1);
    assign io0_oe = !cs_n && !first && oe0;

    // ---------------------------------------------------------------- system domain
    logic [1:0] cs_s, tgl_s;
    logic       tgl_seen, cs_q;
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            cs_s <= 2'b11; tgl_s <= '0; tgl_seen <= 1'b0; cs_q <= 1'b1; o_win <= 1'b0;
        end else begin
            cs_s  <= {cs_s[0], cs_n};
            tgl_s <= {tgl_s[0], win_tgl};
            cs_q  <= cs_s[1];
            o_win <= 1'b0;
            if (cs_s[1] && cs_q && tgl_s[1] != tgl_seen) begin   // a window ended and CS_n stays high
                tgl_seen <= tgl_s[1];
                o_win    <= 1'b1;
            end
        end
    end
    assign o_cs_idle      = cs_s[1] && cs_q;
    assign o_win_cmd      = sum_cmd;
    assign o_win_count    = sum_count;
    assign o_win_rx_ok    = lat_rx_ok;
    assign o_win_rx_sel   = lat_rx_sel;
    assign o_win_tx_valid = lat_tx_valid;
    assign o_win_tx_sel   = lat_tx_sel;

    assign o_rx_rdata  = i_rx_rbuf ? rx_buf1[i_rx_raddr] : rx_buf0[i_rx_raddr];
    assign o_tx_rdata1 = tx_buf1[i_tx_raddr1];
    always_ff @(posedge clk) begin
        if (i_tx_we) begin
            if (i_tx_wbuf) tx_buf1[i_tx_waddr] <= i_tx_wdata;
            else           tx_buf0[i_tx_waddr] <= i_tx_wdata;
        end
    end

    /* verilator lint_off UNUSEDSIGNAL */
    wire _unused = &{1'b0, cmd_sr[6]};
    /* verilator lint_on UNUSEDSIGNAL */
endmodule

`default_nettype wire
