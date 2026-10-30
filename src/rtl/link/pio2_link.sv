// Copyright (C) 2026 Tim Michals
// SPDX-License-Identifier: GPL-3.0-or-later

`default_nettype none

// SPI link, system side: frame buffers' bookkeeping, CRC32, frame handling and the link registers.
//
// Received frames (WRITE_FRAME windows) are checked whole (length, reserved, CRC32) before anything
// happens ([SPEC-LINK-02]); then:
//   REG_WRITE / REG_READ: AXI4-Lite transactions on the core (or the link's own registers at
//                         0x3100..0x31FF), answered by ACK / NAK / REG_READ in reply buffer 0;
//   STATUS:               core ISR, link ISR, queue levels, DMA LEVEL of channels 1..9;
//   STREAM_DATA (2..9):   credit check (DMA LEVEL free count), then the words to the core's input
//                         stream (TDEST = channel), or NAK 4 ([SPEC-LINK-04]).
// Blocks from the core's output stream become STREAM_DATA frames in buffer 1. READ_FRAME windows
// take buffer 0 (replies) before buffer 1 ([SPEC-LINK-08]).
// @impl [SPEC-LINK-02] [SPEC-LINK-03] [SPEC-LINK-04] [SPEC-LINK-05] [SPEC-LINK-07] [SPEC-LINK-08] docs/INTERCONNECT_SPECIFICATION.md
module pio2_link (
    input  wire          clk,
    input  wire          rst_n,

    // PHY ([SPEC-LINK-01])
    output logic [7:0]   o_isr,
    output logic         o_rx_ok,
    output logic         o_rx_sel,
    output logic         o_tx_valid,
    output logic         o_tx_sel,
    output logic [7:0]   o_tx_len0,
    output logic [7:0]   o_tx_len1,
    output logic         o_rx_rbuf,
    output logic [6:0]   o_rx_raddr,
    input  wire  [7:0]   i_rx_rdata,
    output logic         o_tx_we,
    output logic         o_tx_wbuf,
    output logic [6:0]   o_tx_waddr,
    output logic [7:0]   o_tx_wdata,
    output logic [6:0]   o_tx_raddr1,
    input  wire  [7:0]   i_tx_rdata1,
    input  wire          i_cs_idle,
    input  wire          i_win,
    input  wire  [7:0]   i_win_cmd,
    input  wire  [7:0]   i_win_count,
    input  wire          i_win_rx_ok,
    input  wire          i_win_rx_sel,
    input  wire          i_win_tx_valid,
    input  wire          i_win_tx_sel,

    // AXI4-Lite master to the core
    output logic [31:0]  m_axil_awaddr,
    output logic         m_axil_awvalid,
    input  wire          m_axil_awready,
    output logic [31:0]  m_axil_wdata,
    output logic [3:0]   m_axil_wstrb,
    output logic         m_axil_wvalid,
    input  wire          m_axil_wready,
    input  wire  [1:0]   m_axil_bresp,
    input  wire          m_axil_bvalid,
    output logic         m_axil_bready,
    output logic [31:0]  m_axil_araddr,
    output logic         m_axil_arvalid,
    input  wire          m_axil_arready,
    input  wire  [31:0]  m_axil_rdata,
    input  wire  [1:0]   m_axil_rresp,
    input  wire          m_axil_rvalid,
    output logic         m_axil_rready,

    // the core's streams
    output logic [31:0]  m_axis_tdata,
    output logic         m_axis_tvalid,
    input  wire          m_axis_tready,
    output logic         m_axis_tlast,
    output logic [3:0]   m_axis_tdest,
    input  wire  [31:0]  s_axis_tdata,
    input  wire          s_axis_tvalid,
    output logic         s_axis_tready,
    input  wire          s_axis_tlast,
    input  wire  [3:0]   s_axis_tdest,

    input  wire          i_core_irq,
    output logic         o_int_n
);
    localparam logic [7:0] OP_REG_READ = 8'h01, OP_REG_WRITE = 8'h02, OP_STREAM = 8'h03,
                           OP_STATUS = 8'h04, OP_ACK = 8'h05, OP_NAK = 8'h06;
    localparam logic [6:0] CMD_WRITE = 7'h01, CMD_READ = 7'h02;

    function automatic logic [31:0] crc_byte(input logic [31:0] c, input logic [7:0] d);
        logic [31:0] x;
        x = c ^ {24'd0, d};
        for (int i = 0; i < 8; i++) x = x[0] ? (x >> 1) ^ 32'hEDB8_8320 : x >> 1;
        return x;
    endfunction
    function automatic logic [31:0] sat_inc(input logic [31:0] v);
        return &v ? v : v + 32'd1;
    endfunction

    // ---------------------------------------------------------------- buffers
    logic [1:0] rx_full;
    logic [7:0] rx_count [2];
    logic       rx_wsel, rx_next;              // next buffer for a WRITE_FRAME / oldest full one
    logic [1:0] tx_full;                       // 0: replies, 1: stream frames
    logic [7:0] tx_len [2];
    logic       reset_q;

    // link registers
    logic [31:0] crc_err, len_err, overflow, dropped;
    logic        crc_sticky, ovf_sticky;
    logic [7:0]  imr;
    wire  [7:0]  isr_now = {1'b0, ovf_sticky, crc_sticky, 2'b00, i_core_irq,
                            !(rx_full[0] && rx_full[1]), |tx_full};
    assign o_int_n = !(i_core_irq || |(isr_now & imr));

    // offer to the PHY: changes only while CS_n is high ([SPEC-LINK-01])
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            o_isr <= '0; o_rx_ok <= 1'b0; o_rx_sel <= 1'b0; o_tx_valid <= 1'b0; o_tx_sel <= 1'b0;
            o_tx_len0 <= '0; o_tx_len1 <= '0;
        end else if (i_cs_idle && !i_win) begin
            o_isr      <= isr_now;
            o_rx_ok    <= !rx_full[rx_wsel];
            o_rx_sel   <= rx_wsel;
            o_tx_sel   <= !tx_full[0];
            o_tx_valid <= tx_full[0] || tx_full[1];
            o_tx_len0  <= tx_len[0];
            o_tx_len1  <= tx_len[1];
        end
    end

    // ---------------------------------------------------------------- frame handler
    typedef enum logic [4:0] {
        S_IDLE, S_CHECK, S_CHECKED, S_WORD, S_WR_BUS, S_WR_RESP, S_RD_BUS, S_RD_RESP, S_PUT_WORD,
        S_STATUS, S_CREDIT, S_CREDIT_RESP, S_PUSH, S_REPLY_HDR, S_REPLY_CRC, S_DONE
    } state_t;
    state_t st, ret;

    logic [6:0]  ri;                           // byte index in the received frame
    logic [31:0] crc;
    logic [7:0]  h_op, h_chan, h_flags, h_seq;
    logic [15:0] h_len;
    logic [15:0] h_res;
    logic [31:0] h_addr, h_crc;
    logic [7:0]  flen;                         // whole frame length from the header
    logic [5:0]  n, k;                         // words to handle / done
    logic [31:0] word, addr;
    logic [1:0]  wb;                           // byte of the word being assembled
    logic [31:0] err;                          // NAK code, 0 = none
    logic [7:0]  r_op;
    logic [6:0]  wi;                           // reply byte index
    logic [31:0] wcrc;
    logic [3:0]  si;                           // STATUS word index
    logic [31:0] status [3];

    wire has_addr = h_op == OP_REG_READ || h_op == OP_REG_WRITE;
    wire [7:0] hdr_bytes = has_addr ? 8'd12 : 8'd8;
    wire local_reg = addr[31:8] == 24'h0000_31;

    // local register reads / writes (0x3100..0x31FF)
    logic [31:0] local_rdata;
    always_comb begin
        case (addr[7:0])
            8'h00: local_rdata = {16'd0, 8'd2, 8'd0};
            8'h04: local_rdata = {24'd0, isr_now};
            8'h08: local_rdata = {24'd0, imr};
            8'h0C: local_rdata = {30'd0, 2'(tx_full[0]) + 2'(tx_full[1])};
            8'h10: local_rdata = {30'd0, 2'(rx_full[0]) + 2'(rx_full[1])};
            8'h14: local_rdata = tx_full[0] ? {8'd0, 8'd0, 8'(tx_len[0] - 8'd12)}
                               : tx_full[1] ? {8'd0, stream_chan, 8'(tx_len[1] - 8'd12)} : 32'd0;
            8'h18: local_rdata = crc_err;
            8'h1C: local_rdata = len_err;
            8'h20: local_rdata = overflow;
            8'h24: local_rdata = dropped;
            default: local_rdata = 32'd0;
        endcase
    end
    logic        local_we;
    logic [7:0]  stream_chan;

    // reply bytes: header, then words; one byte per clock into tx buffer 0
    logic [7:0] rb [12];
    always_comb begin
        rb[0] = r_op; rb[1] = 8'd0; rb[2] = 8'd0; rb[3] = h_seq;
        rb[4] = (r_op == OP_REG_READ) ? 8'({n, 2'b00}) : (r_op == OP_STATUS ? 8'd48 : 8'd4);
        rb[5] = 8'd0; rb[6] = 8'd0; rb[7] = 8'd0;
        rb[8] = h_addr[7:0]; rb[9] = h_addr[15:8]; rb[10] = h_addr[23:16]; rb[11] = h_addr[31:24];
    end
    wire [7:0] r_hdr_len = (r_op == OP_REG_READ) ? 8'd12 : 8'd8;

    // the byte the reply writer puts out this clock
    logic       put;
    logic [7:0] put_byte;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            st <= S_IDLE; ret <= S_IDLE;
            rx_full <= '0; rx_count[0] <= '0; rx_count[1] <= '0; rx_wsel <= 1'b0; rx_next <= 1'b0;
            tx_full[0] <= 1'b0; tx_len[0] <= '0;
            crc_err <= '0; len_err <= '0; overflow <= '0; dropped <= '0;
            crc_sticky <= 1'b0; ovf_sticky <= 1'b0; imr <= '0; reset_q <= 1'b0;
            ri <= '0; crc <= '1; h_op <= '0; h_chan <= '0; h_flags <= '0; h_seq <= '0; h_len <= '0;
            h_res <= '0; h_addr <= '0; h_crc <= '0; flen <= '0; n <= '0; k <= '0; word <= '0;
            addr <= '0; wb <= '0; err <= '0; r_op <= '0; wi <= '0; wcrc <= '1; si <= '0;
            for (int i = 0; i < 3; i++) status[i] <= '0;
            m_axil_awvalid <= 1'b0; m_axil_wvalid <= 1'b0; m_axil_arvalid <= 1'b0;
            m_axil_awaddr <= '0; m_axil_wdata <= '0; m_axil_araddr <= '0;
            m_axis_tvalid <= 1'b0; m_axis_tdata <= '0; m_axis_tlast <= 1'b0; m_axis_tdest <= '0;
        end else begin
            // ---- windows that ended
            if (i_win) begin
                if (i_win_cmd[6:0] == CMD_WRITE && i_win_count != 8'd0) begin
                    if (i_win_rx_ok) begin
                        rx_full[i_win_rx_sel]  <= 1'b1;
                        rx_count[i_win_rx_sel] <= i_win_count;
                        rx_wsel <= !i_win_rx_sel;
                        if (!rx_full[!i_win_rx_sel] && !(st != S_IDLE)) rx_next <= i_win_rx_sel;
                    end else begin
                        overflow <= sat_inc(overflow); ovf_sticky <= 1'b1;
                    end
                end
                if (i_win_cmd[6:0] == CMD_READ && i_win_tx_valid && !i_win_tx_sel && tx_full[0] &&
                    i_win_count >= tx_len[0])
                    tx_full[0] <= 1'b0;
            end

            // ---- link register writes (from a REG_WRITE frame)
            if (local_we) begin
                case (addr[7:0])
                    8'h00: if (word[1]) reset_q <= 1'b1;
                    8'h04: begin
                        if (word[5]) crc_sticky <= 1'b0;
                        if (word[6]) ovf_sticky <= 1'b0;
                    end
                    8'h08: imr <= word[7:0];
                    8'h18: crc_err <= '0;
                    8'h1C: len_err <= '0;
                    8'h20: overflow <= '0;
                    8'h24: dropped <= '0;
                    default: ;
                endcase
            end

            case (st)
                S_IDLE: begin
                    if (reset_q) begin
                        reset_q <= 1'b0;
                        rx_full <= '0; tx_full[0] <= 1'b0;
                    end else if (rx_full[rx_next]) begin
                        st <= S_CHECK; ri <= '0; crc <= '1; err <= '0;
                    end else if (rx_full[!rx_next]) begin
                        rx_next <= !rx_next;
                    end
                end
                S_CHECK: begin                              // one byte per clock: header, CRC
                    case (ri)
                        7'd0: h_op <= i_rx_rdata;
                        7'd1: h_chan <= i_rx_rdata;
                        7'd2: h_flags <= i_rx_rdata;
                        7'd3: h_seq <= i_rx_rdata;
                        7'd4: h_len[7:0] <= i_rx_rdata;
                        7'd5: h_len[15:8] <= i_rx_rdata;
                        7'd6: h_res[7:0] <= i_rx_rdata;
                        7'd7: h_res[15:8] <= i_rx_rdata;
                        default: ;
                    endcase
                    if (ri >= 7'd8 && ri < 7'd12 && has_addr) h_addr[8 * (ri - 7'd8) +: 8] <= i_rx_rdata;
                    if (ri == 7'd8)
                        flen <= 8'(h_len) + hdr_bytes + 8'd4;
                    if (ri >= 7'd8 && {1'b0, ri} >= flen - 8'd4 && {1'b0, ri} < flen)
                        h_crc[8 * (ri - 7'(flen - 8'd4)) +: 8] <= i_rx_rdata;
                    if (ri < 7'd8 || {1'b0, ri} < flen - 8'd4) crc <= crc_byte(crc, i_rx_rdata);
                    if (ri == 7'd8 && (h_len > 16'd116 || h_len[1:0] != 2'd0 || h_res != 16'd0 ||
                                       8'(h_len) + hdr_bytes + 8'd4 > rx_count[rx_next] ||
                                       (has_addr && h_len > 16'd112))) begin
                        len_err <= sat_inc(len_err);
                        st <= S_DONE;
                    end else if (ri >= 7'd8 && {1'b0, ri} == flen - 8'd1) begin
                        st <= S_CHECKED;
                    end
                    ri <= ri + 7'd1;
                end
                S_CHECKED: begin
                    if ((crc ^ 32'hFFFF_FFFF) != h_crc) begin
                        crc_err <= sat_inc(crc_err); crc_sticky <= 1'b1;
                        st <= S_DONE;
                    end else begin
                        n <= 6'(h_len >> 2); k <= '0; addr <= h_addr; ri <= 7'(hdr_bytes); wb <= '0;
                        case (h_op)
                            OP_REG_WRITE: begin
                                if (h_chan != 8'd0 || h_len == 16'd0) begin err <= 32'd2; st <= S_REPLY_HDR; r_op <= OP_NAK; end
                                else begin r_op <= OP_ACK; ret <= S_WR_BUS; st <= S_WORD; end
                            end
                            OP_REG_READ: begin
                                if (h_chan != 8'd0 || h_len != 16'd4) begin err <= 32'd5; st <= S_REPLY_HDR; r_op <= OP_NAK; end
                                else begin ret <= S_CHECKED; st <= S_WORD; r_op <= OP_REG_READ; end
                            end
                            OP_STATUS: begin r_op <= OP_STATUS; si <= '0; st <= S_STATUS; end
                            OP_STREAM: begin
                                if (h_chan < 8'd2 || h_chan > 8'd9) begin err <= 32'd2; st <= S_REPLY_HDR; r_op <= OP_NAK; end
                                else if (h_len == 16'd0) begin err <= 32'd5; st <= S_REPLY_HDR; r_op <= OP_NAK; end
                                else st <= S_CREDIT;
                            end
                            default: begin err <= 32'd1; st <= S_REPLY_HDR; r_op <= OP_NAK; end
                        endcase
                        if (h_op == OP_REG_READ && h_chan == 8'd0 && h_len == 16'd4) ret <= S_RD_BUS;
                    end
                end
                S_WORD: begin                               // payload word k, little endian
                    word[8 * wb +: 8] <= i_rx_rdata;
                    ri <= ri + 7'd1;
                    wb <= wb + 2'd1;
                    if (wb == 2'd3) st <= ret;
                end
                S_WR_BUS: begin
                    if (local_reg) begin
                        st <= S_WR_RESP;
                    end else if (!m_axil_awvalid && !m_axil_wvalid) begin
                        m_axil_awvalid <= 1'b1; m_axil_awaddr <= addr;
                        m_axil_wvalid <= 1'b1;  m_axil_wdata <= word;
                        st <= S_WR_RESP;
                    end
                end
                S_WR_RESP: begin
                    if (m_axil_awvalid && m_axil_awready) m_axil_awvalid <= 1'b0;
                    if (m_axil_wvalid && m_axil_wready)   m_axil_wvalid <= 1'b0;
                    if (local_reg || m_axil_bvalid) begin
                        if (!local_reg && m_axil_bresp != 2'b00 && err == 32'd0)
                            err <= 32'd3 | {22'd0, m_axil_bresp, 8'd0};
                        k <= k + 6'd1;
                        if (!h_flags[0]) addr <= addr + 32'd4;
                        if (k + 6'd1 == n) begin
                            st <= S_REPLY_HDR;
                            r_op <= OP_ACK;
                        end else begin
                            st <= S_WORD; ret <= S_WR_BUS;
                        end
                    end
                end
                S_RD_BUS: begin                             // first entry: word = count
                    if (k == 6'd0 && ret == S_RD_BUS && n == 6'd1 && wi == 7'd0) begin
                        if (word == 32'd0 || word > 32'd28) begin
                            err <= 32'd5; r_op <= OP_NAK; st <= S_REPLY_HDR;
                        end else begin
                            n <= 6'(word); ret <= S_RD_RESP;     // count read, header goes out first
                            r_op <= OP_REG_READ; st <= S_REPLY_HDR;
                        end
                    end else if (local_reg) begin
                        word <= local_rdata; st <= S_PUT_WORD;
                    end else if (!m_axil_arvalid) begin
                        m_axil_arvalid <= 1'b1; m_axil_araddr <= addr; st <= S_RD_RESP;
                    end
                end
                S_RD_RESP: begin
                    if (m_axil_arvalid && m_axil_arready) m_axil_arvalid <= 1'b0;
                    if (m_axil_rvalid) begin
                        word <= m_axil_rdata;
                        if (m_axil_rresp != 2'b00 && err == 32'd0) err <= 32'd3 | {22'd0, m_axil_rresp, 8'd0};
                        st <= S_PUT_WORD;
                    end
                end
                S_PUT_WORD: begin                           // 4 bytes of `word` into the reply
                    if (wb == 2'd3) begin
                        k <= k + 6'd1;
                        if (!h_flags[0] && r_op == OP_REG_READ) addr <= addr + 32'd4;
                        if (r_op == OP_STATUS) begin
                            si <= si + 4'd1;
                            st <= (si == 4'd11) ? S_REPLY_CRC : S_STATUS;
                        end else if (k + 6'd1 == n) begin
                            st <= S_REPLY_CRC;
                        end else begin
                            st <= S_RD_BUS;
                        end
                    end
                    wb <= wb + 2'd1;
                end
                S_STATUS: begin
                    if (si == 4'd0 && wi == 7'd0) begin
                        st <= S_REPLY_HDR;                  // header first
                    end else if (si == 4'd0) begin
                        if (!m_axil_arvalid && !m_axil_rvalid) begin
                            m_axil_arvalid <= 1'b1; m_axil_araddr <= 32'h0000_3008;
                        end
                        if (m_axil_arvalid && m_axil_arready) m_axil_arvalid <= 1'b0;
                        if (m_axil_rvalid) begin word <= m_axil_rdata; wb <= '0; st <= S_PUT_WORD; end
                    end else if (si == 4'd1) begin
                        word <= {24'd0, isr_now}; wb <= '0; st <= S_PUT_WORD;
                    end else if (si == 4'd2) begin
                        word <= {16'd0, 6'd0, 2'(tx_full[0]) + 2'(tx_full[1]), 6'd0, 2'(rx_full[0]) + 2'(rx_full[1])};
                        wb <= '0; st <= S_PUT_WORD;
                    end else begin                          // DMA LEVEL of channel si - 2
                        if (!m_axil_arvalid && !m_axil_rvalid) begin
                            m_axil_arvalid <= 1'b1;
                            m_axil_araddr <= 32'h0000_1008 + {22'd0, si - 4'd2, 6'd0};
                        end
                        if (m_axil_arvalid && m_axil_arready) m_axil_arvalid <= 1'b0;
                        if (m_axil_rvalid) begin word <= m_axil_rdata; wb <= '0; st <= S_PUT_WORD; end
                    end
                end
                S_CREDIT: begin
                    if (!m_axil_arvalid) begin
                        m_axil_arvalid <= 1'b1; m_axil_araddr <= 32'h0000_1008 + {18'd0, h_chan, 6'd0};
                        st <= S_CREDIT_RESP;
                    end
                end
                S_CREDIT_RESP: begin
                    if (m_axil_arvalid && m_axil_arready) m_axil_arvalid <= 1'b0;
                    if (m_axil_rvalid) begin
                        if ({10'd0, n} > m_axil_rdata[31:16] || m_axil_rresp != 2'b00) begin
                            overflow <= sat_inc(overflow); ovf_sticky <= 1'b1;
                            err <= 32'd4; r_op <= OP_NAK; st <= S_REPLY_HDR;
                        end else begin
                            ret <= S_PUSH; st <= S_WORD;
                        end
                    end
                end
                S_PUSH: begin
                    if (!m_axis_tvalid) begin
                        m_axis_tvalid <= 1'b1; m_axis_tdata <= word; m_axis_tdest <= h_chan[3:0];
                        m_axis_tlast <= k + 6'd1 == n;
                    end else if (m_axis_tready) begin
                        m_axis_tvalid <= 1'b0;
                        k <= k + 6'd1;
                        if (k + 6'd1 == n) st <= S_DONE;
                        else begin ret <= S_PUSH; st <= S_WORD; end
                    end
                end
                S_REPLY_HDR: begin                          // waits for the reply buffer, then the header
                    if (wi == 7'd0 && tx_full[0]) begin
                        // wait until the host has read the previous reply
                    end else begin
                        wi <= wi + 7'd1;
                        if (wi == 7'd0) wcrc <= crc_byte(32'hFFFF_FFFF, rb[0]);
                        else            wcrc <= crc_byte(wcrc, rb[wi[3:0]]);
                        if ({1'b0, wi} == r_hdr_len - 8'd1) begin
                            wb <= '0;
                            if (r_op == OP_ACK || r_op == OP_NAK) begin
                                word <= err; st <= S_PUT_WORD; n <= 6'd1; k <= '0;
                            end else if (r_op == OP_STATUS) begin
                                si <= '0; st <= S_STATUS;
                            end else begin                  // REG_READ: the words
                                k <= '0; addr <= h_addr; st <= S_RD_BUS;
                            end
                        end
                    end
                end
                S_REPLY_CRC: begin                          // 4 CRC bytes, then offer the reply
                    wi <= wi + 7'd1;
                    if (wb == 2'd3) begin
                        tx_len[0] <= 8'(wi) + 8'd1;
                        tx_full[0] <= 1'b1;
                        st <= S_DONE;
                    end
                    wb <= wb + 2'd1;
                end
                S_DONE: begin
                    if (h_op == OP_REG_WRITE || h_op == OP_REG_READ || h_op == OP_STATUS || h_op == OP_STREAM) begin
                        if (r_op == OP_NAK && (err == 32'd1 || err == 32'd2 || err == 32'd5)) dropped <= sat_inc(dropped);
                    end
                    rx_full[rx_next] <= 1'b0;
                    rx_next <= !rx_next;
                    wi <= '0; r_op <= '0; ret <= S_IDLE;
                    st <= S_IDLE;
                end
                default: st <= S_IDLE;
            endcase
            if (st == S_PUT_WORD || st == S_REPLY_CRC) begin
                if (!(st == S_REPLY_CRC)) wcrc <= crc_byte(wcrc, put_byte);
            end
        end
    end

    // S_WR_RESP for a local register: the write happens in that clock
    assign local_we = st == S_WR_RESP && local_reg;

    // reply byte writer: header bytes, word bytes, CRC bytes into tx buffer 0
    always_comb begin
        put = 1'b0;
        put_byte = 8'd0;
        if (st == S_REPLY_HDR && !(wi == 7'd0 && tx_full[0])) begin
            put = 1'b1; put_byte = rb[wi[3:0]];
        end else if (st == S_PUT_WORD) begin
            put = 1'b1; put_byte = word[8 * wb +: 8];
        end else if (st == S_REPLY_CRC) begin
            put = 1'b1; put_byte = ~wcrc[8 * wb +: 8];
        end
    end
    // In S_PUT_WORD the write index advances with the bytes
    logic [6:0] put_idx;
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) put_idx <= '0;
        else if (st == S_REPLY_HDR && wi == 7'd0 && !tx_full[0]) put_idx <= 7'd1;
        else if (put) put_idx <= put_idx + 7'd1;
    end

    assign m_axil_wstrb  = 4'hF;
    assign m_axil_bready = 1'b1;
    assign m_axil_rready = 1'b1;
    assign o_rx_rbuf  = rx_next;
    assign o_rx_raddr = (st == S_CHECK || st == S_WORD) ? ri : 7'd0;

    // ---------------------------------------------------------------- stream frames out (buffer 1)
    typedef enum logic [2:0] {T_IDLE, T_WORDS, T_HDR, T_CRC, T_CRCOUT} tstate_t;
    tstate_t ts;
    logic [6:0]  ti;                 // byte index in buffer 1
    logic [5:0]  tw;                 // words taken
    logic [1:0]  tb;
    logic [31:0] tword, tcrc;
    logic [7:0]  out_seq;
    logic [7:0]  t_byte;
    logic        t_we;
    logic [6:0]  t_addr;

    assign s_axis_tready = ts == T_WORDS && tb == 2'd0 && !tvalid_held;
    logic tvalid_held;               // a word taken, its bytes being written

    wire [7:0] thdr [8];
    assign thdr[0] = OP_STREAM;
    assign thdr[1] = {4'd0, stream_chan[3:0]};
    assign thdr[2] = 8'd0;
    assign thdr[3] = out_seq;
    assign thdr[4] = {tw, 2'b00};
    assign thdr[5] = 8'd0;
    assign thdr[6] = 8'd0;
    assign thdr[7] = 8'd0;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            ts <= T_IDLE; ti <= '0; tw <= '0; tb <= '0; tword <= '0; tcrc <= '1; out_seq <= '0;
            stream_chan <= '0; tvalid_held <= 1'b0; tx_full[1] <= 1'b0; tx_len[1] <= '0;
        end else begin
            if (i_win && i_win_cmd[6:0] == CMD_READ && i_win_tx_valid && i_win_tx_sel && tx_full[1] &&
                i_win_count >= tx_len[1])
                tx_full[1] <= 1'b0;
            if (st == S_IDLE && reset_q) tx_full[1] <= 1'b0;
            case (ts)
                T_IDLE: if (!tx_full[1] && s_axis_tvalid) begin
                    ts <= T_WORDS; ti <= 7'd8; tw <= '0; tb <= '0; stream_chan <= {4'd0, s_axis_tdest};
                end
                T_WORDS: begin
                    if (!tvalid_held) begin
                        if (s_axis_tvalid) begin
                            tword <= s_axis_tdata; tvalid_held <= 1'b1; tb <= 2'd0;
                            tw <= tw + 6'd1;
                            if (s_axis_tlast || tw + 6'd1 == 6'd29) tcrc <= 32'd1;   // marks "last"
                            else tcrc <= 32'd0;
                        end
                    end else begin
                        ti <= ti + 7'd1;
                        tb <= tb + 2'd1;
                        if (tb == 2'd3) begin
                            tvalid_held <= 1'b0;
                            if (tcrc == 32'd1) begin ts <= T_HDR; ti <= '0; end
                        end
                    end
                end
                T_HDR: begin
                    ti <= ti + 7'd1;
                    if (ti == 7'd7) begin ts <= T_CRC; ti <= '0; tcrc <= '1; end
                end
                T_CRC: begin                         // read back header and payload for the CRC
                    tcrc <= crc_byte(tcrc, i_tx_rdata1);
                    ti <= ti + 7'd1;
                    if (ti == 7'(8 + 4 * tw - 1)) begin ts <= T_CRCOUT; ti <= 7'(8 + 4 * tw); tb <= '0; end
                end
                T_CRCOUT: begin
                    ti <= ti + 7'd1;
                    tb <= tb + 2'd1;
                    if (tb == 2'd3) begin
                        tx_len[1] <= 8'(ti) + 8'd1;
                        tx_full[1] <= 1'b1;
                        out_seq <= out_seq + 8'd1;
                        ts <= T_IDLE;
                    end
                end
                default: ts <= T_IDLE;
            endcase
        end
    end
    assign o_tx_raddr1 = ti;
    always_comb begin
        t_we = 1'b0; t_byte = 8'd0; t_addr = ti;
        case (ts)
            T_WORDS:  if (tvalid_held) begin t_we = 1'b1; t_byte = tword[8 * tb +: 8]; end
            T_HDR:    begin t_we = 1'b1; t_byte = thdr[ti[2:0]]; end
            T_CRCOUT: begin t_we = 1'b1; t_byte = ~tcrc[8 * tb +: 8]; end
            default: ;
        endcase
    end

    // tx buffer write port: replies to buffer 0, stream frames to buffer 1 (one each clock: the
    // stream writer waits while a reply byte goes out)
    always_comb begin
        if (put) begin
            o_tx_we = 1'b1; o_tx_wbuf = 1'b0; o_tx_waddr = (st == S_REPLY_HDR) ? wi : put_idx;
            o_tx_wdata = put_byte;
        end else begin
            o_tx_we = t_we; o_tx_wbuf = 1'b1; o_tx_waddr = t_addr; o_tx_wdata = t_byte;
        end
    end

    /* verilator lint_off UNUSEDSIGNAL */
    wire _unused = &{1'b0, status[0], h_flags[7:1], i_win_cmd[7], r_hdr_len};
    /* verilator lint_on UNUSEDSIGNAL */
endmodule

`default_nettype wire
