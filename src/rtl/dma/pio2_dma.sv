// Copyright (C) 2026 Tim Michals
// SPDX-License-Identifier: GPL-3.0-or-later

`default_nettype none

// DMA engine: eight PIO channels (link channels 2..9) and the trace channel (link channel 1), their
// registers (window 0x1000), the routing to the tile's FIFO stream ports, the input split by TDEST
// and the output merge (Forencich axis_arb_mux, whole TLAST blocks, round robin).
// @impl [SPEC-DMA-01] [SPEC-DMA-02] [SPEC-TRC-02] docs/INTERCONNECT_SPECIFICATION.md
module pio2_dma #(
    parameter int RING_WORDS       = 256,
    parameter int TRACE_RING_WORDS = 512,
    parameter int FRAME_WORDS      = 28
) (
    input  wire          clk,
    input  wire          rst_n,

    // registers (window 0x1000)
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

    // the tile's FIFO stream ports ([SPEC-ICN-09]) and what the triggers watch
    output logic [127:0] m_tx_tdata,
    output logic [3:0]   m_tx_tvalid,
    input  wire  [3:0]   m_tx_tready,
    input  wire  [127:0] s_rx_tdata,
    input  wire  [3:0]   s_rx_tvalid,
    output logic [3:0]   s_rx_tready,
    input  wire  [3:0]   i_tx_empty,
    input  wire  [7:0]   i_irq,
    input  wire  [31:0]  i_gpio,

    // trace records from the event FIFO ([SPEC-TRC-01])
    input  wire  [63:0]  s_trace_tdata,
    input  wire          s_trace_tvalid,
    output logic         s_trace_tready,

    // the core's streams ([SPEC-ICN-11]): channel in TDEST
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

    // to the core registers; bit c-1 is channel c
    input  wire  [8:0]   i_done_flags,
    output logic [8:0]   o_done,
    output logic [8:0]   o_err,
    output logic [8:0]   o_pend,
    output logic         o_in_drop,

    // eight trace producers, one per PIO channel
    output logic [7:0]   o_ev_valid,
    output logic [63:0]  o_ev_type,
    output logic [63:0]  o_ev_src,
    output logic [127:0] o_ev_data,
    output logic [7:0]   o_ev_merged
);
    localparam int NC = 9;          // index k = channel - 1: k 0 is the trace, k 1..8 are PIO channels

    // ---------------------------------------------------------------- registers
    logic          wr, rd;
    logic [11:0]   waddr, raddr;
    logic [31:0]   wdata, rdata;

    pio2_axil_regs #(.AW(12)) u_regs (
        .clk(clk), .rst_n(rst_n),
        .s_axil_awaddr(s_axil_awaddr), .s_axil_awvalid(s_axil_awvalid), .s_axil_awready(s_axil_awready),
        .s_axil_wdata(s_axil_wdata), .s_axil_wstrb(s_axil_wstrb), .s_axil_wvalid(s_axil_wvalid),
        .s_axil_wready(s_axil_wready), .s_axil_bresp(s_axil_bresp), .s_axil_bvalid(s_axil_bvalid),
        .s_axil_bready(s_axil_bready), .s_axil_araddr(s_axil_araddr), .s_axil_arvalid(s_axil_arvalid),
        .s_axil_arready(s_axil_arready), .s_axil_rdata(s_axil_rdata), .s_axil_rresp(s_axil_rresp),
        .s_axil_rvalid(s_axil_rvalid), .s_axil_rready(s_axil_rready),
        .o_wr(wr), .o_waddr(waddr), .o_wdata(wdata), .o_rd(rd), .o_raddr(raddr), .i_rdata(rdata)
    );

    logic [31:0] cfg   [NC];
    logic [15:0] count [NC];
    logic [23:0] flush [NC];
    logic [15:0] thr   [NC];
    logic [NC-1:0] start, reset_ring, sw_trig, clr_moved, clr_under, clr_over, clr_trig;

    wire [5:0] wblk = waddr[11:6];
    wire [5:0] woff = waddr[5:0];

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            for (int k = 0; k < NC; k++) begin
                cfg[k] <= '0; count[k] <= 16'd1; flush[k] <= '0; thr[k] <= '0;
            end
            start <= '0; reset_ring <= '0; sw_trig <= '0;
            clr_moved <= '0; clr_under <= '0; clr_over <= '0; clr_trig <= '0;
        end else begin
            start <= '0; reset_ring <= '0; sw_trig <= '0;
            clr_moved <= '0; clr_under <= '0; clr_over <= '0; clr_trig <= '0;
            if (wr && wblk >= 6'd1 && wblk <= 6'(NC)) begin
                for (int k = 0; k < NC; k++) if (wblk == 6'(k + 1)) begin
                    case (woff)
                        6'h00: cfg[k]   <= wdata;
                        6'h04: count[k] <= wdata[15:0];
                        6'h10: clr_moved[k] <= 1'b1;
                        6'h14: clr_under[k] <= 1'b1;
                        6'h18: clr_over[k]  <= 1'b1;
                        6'h1C: clr_trig[k]  <= 1'b1;
                        6'h20: flush[k] <= wdata[23:0];
                        6'h24: thr[k]   <= wdata[15:0];
                        6'h28: begin
                            reset_ring[k] <= wdata[0];
                            start[k]      <= wdata[1];
                            sw_trig[k]    <= wdata[2];
                        end
                        default: ;
                    endcase
                end
            end
        end
    end

    // ---------------------------------------------------------------- triggers
    logic [7:0]  irq_q;
    logic [31:0] gpio_q;
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin irq_q <= '0; gpio_q <= '0; end
        else        begin irq_q <= i_irq; gpio_q <= i_gpio; end
    end
    wire [7:0]  irq_rise  = i_irq & ~irq_q;
    wire [31:0] gpio_rise = i_gpio & ~gpio_q;

    // ---------------------------------------------------------------- channels
    logic [NC-1:0] en, dir;
    logic [1:0]    mode [NC];
    logic [1:0]    sm   [NC];
    logic [NC-1:0] trig;
    always_comb begin
        for (int k = 0; k < NC; k++) begin
            en[k]   = cfg[k][0];
            dir[k]  = k != 0 && cfg[k][1];                      // the trace channel is always OUT
            mode[k] = k == 0 ? 2'd1 : cfg[k][3:2];              // ... in RING mode
            sm[k]   = cfg[k][5:4];
            trig[k] = sw_trig[k] || (cfg[k][6] ? gpio_rise[cfg[k][12:8]] : irq_rise[cfg[k][10:8]]);
        end
    end

    logic [31:0] in_tdata  [NC];
    logic [NC-1:0] in_tvalid, in_tready, in_drop;
    logic [31:0] out_tdata [NC];
    logic [NC-1:0] out_tvalid, out_tready, out_tlast;
    logic [31:0] tx_tdata  [NC];
    logic [NC-1:0] tx_tvalid, tx_tready, tx_empty;
    logic [31:0] rx_tdata  [NC];
    logic [NC-1:0] rx_tvalid, rx_tready;
    logic [15:0] level [NC], free [NC], remaining [NC];
    logic [NC-1:0] busy;
    logic [31:0] moved [NC], underruns [NC], overruns [NC], triggers [NC];
    logic [NC-1:0] ev_valid, ev_merged;
    logic [7:0]  ev_type [NC];
    logic [15:0] ev_data [NC];

    // Trace records, 64 -> 32 bits, bits [31:0] first ([SPEC-TRC-01])
    logic [31:0] trace32_tdata;
    logic        trace32_tvalid, trace32_tready;
    axis_adapter #(
        .S_DATA_WIDTH(64), .S_KEEP_ENABLE(1), .S_KEEP_WIDTH(8),
        .M_DATA_WIDTH(32), .M_KEEP_ENABLE(1), .M_KEEP_WIDTH(4),
        .ID_ENABLE(0), .DEST_ENABLE(0), .USER_ENABLE(0)
    ) u_trace_adapter (
        .clk(clk), .rst(!rst_n),
        .s_axis_tdata(s_trace_tdata), .s_axis_tkeep(8'hFF), .s_axis_tvalid(s_trace_tvalid),
        .s_axis_tready(s_trace_tready), .s_axis_tlast(1'b0), .s_axis_tid(8'd0), .s_axis_tdest(8'd0),
        .s_axis_tuser(1'b0),
        .m_axis_tdata(trace32_tdata), .m_axis_tkeep(), .m_axis_tvalid(trace32_tvalid),
        .m_axis_tready(trace32_tready), .m_axis_tlast(), .m_axis_tid(), .m_axis_tdest(), .m_axis_tuser()
    );

    // Routing to the tile: an SM's TX (RX) FIFO belongs to the lowest enabled IN (OUT) channel on it.
    logic [3:0] tx_own_v, rx_own_v;
    logic [3:0] tx_own [4], rx_own [4];
    always_comb begin
        for (int s = 0; s < 4; s++) begin
            tx_own_v[s] = 1'b0; tx_own[s] = '0;
            rx_own_v[s] = 1'b0; rx_own[s] = '0;
            for (int k = NC - 1; k >= 1; k--) begin
                if (en[k] && sm[k] == 2'(s) && dir[k])  begin tx_own_v[s] = 1'b1; tx_own[s] = 4'(k); end
                if (en[k] && sm[k] == 2'(s) && !dir[k]) begin rx_own_v[s] = 1'b1; rx_own[s] = 4'(k); end
            end
        end
    end
    // One block per direction, so no false combinational loop is seen between them.
    always_comb begin
        for (int s = 0; s < 4; s++) begin
            m_tx_tvalid[s]         = tx_own_v[s] && tx_tvalid[tx_own[s]];
            m_tx_tdata[32*s +: 32] = tx_tdata[tx_own[s]];
        end
    end
    always_comb begin
        for (int s = 0; s < 4; s++) s_rx_tready[s] = rx_own_v[s] && rx_tready[rx_own[s]];
    end
    always_comb begin
        tx_tready[0] = 1'b0;
        for (int k = 1; k < NC; k++)
            tx_tready[k] = tx_own_v[sm[k]] && tx_own[sm[k]] == 4'(k) && m_tx_tready[sm[k]];
    end
    always_comb begin
        rx_tvalid[0] = trace32_tvalid;
        rx_tdata[0]  = trace32_tdata;
        for (int k = 1; k < NC; k++) begin
            rx_tvalid[k] = rx_own_v[sm[k]] && rx_own[sm[k]] == 4'(k) && s_rx_tvalid[sm[k]];
            rx_tdata[k]  = s_rx_tdata[32*sm[k] +: 32];
        end
    end
    always_comb begin
        tx_empty[0] = 1'b0;
        for (int k = 1; k < NC; k++) tx_empty[k] = i_tx_empty[sm[k]];
    end
    assign trace32_tready = rx_tready[0];

    // Input split by TDEST: channels 2..9 -> k 1..8; anything else is dropped and counted.
    wire in_range = s_axis_tdest >= 4'd2 && s_axis_tdest <= 4'd9;
    always_comb begin
        for (int k = 0; k < NC; k++) begin
            in_tdata[k]  = s_axis_tdata;
            in_tvalid[k] = k != 0 && s_axis_tvalid && s_axis_tdest == 4'(k + 1);
        end
        s_axis_tready = in_range ? in_tready[s_axis_tdest - 4'd1] : 1'b1;
        o_in_drop = (s_axis_tvalid && !in_range) || |in_drop;
    end

    generate
        for (genvar k = 0; k < NC; k++) begin : gen_ch
            pio2_dma_chan #(
                .RING_WORDS(k == 0 ? TRACE_RING_WORDS : RING_WORDS), .FRAME_WORDS(FRAME_WORDS)
            ) u_ch (
                .clk(clk), .rst_n(rst_n),
                .i_en(en[k]), .i_dir(dir[k]), .i_mode(mode[k]), .i_count(count[k]), .i_flush(flush[k]),
                .i_thr(thr[k]), .i_start(start[k]), .i_reset_ring(reset_ring[k]), .i_trigger(trig[k]),
                .s_in_tdata(in_tdata[k]), .s_in_tvalid(in_tvalid[k]), .s_in_tready(in_tready[k]),
                .m_out_tdata(out_tdata[k]), .m_out_tvalid(out_tvalid[k]), .m_out_tready(out_tready[k]),
                .m_out_tlast(out_tlast[k]),
                .m_tx_tdata(tx_tdata[k]), .m_tx_tvalid(tx_tvalid[k]), .m_tx_tready(tx_tready[k]),
                .i_tx_empty(tx_empty[k]),
                .s_rx_tdata(rx_tdata[k]), .s_rx_tvalid(rx_tvalid[k]), .s_rx_tready(rx_tready[k]),
                .o_level(level[k]), .o_free(free[k]), .o_busy(busy[k]), .o_remaining(remaining[k]),
                .o_pend(o_pend[k]), .o_done(o_done[k]), .o_err(o_err[k]), .o_in_drop(in_drop[k]),
                .o_moved(moved[k]), .o_underruns(underruns[k]), .o_overruns(overruns[k]),
                .o_triggers(triggers[k]),
                .i_clr_moved(clr_moved[k]), .i_clr_underruns(clr_under[k]), .i_clr_overruns(clr_over[k]),
                .i_clr_triggers(clr_trig[k]),
                .o_ev_valid(ev_valid[k]), .o_ev_type(ev_type[k]), .o_ev_data(ev_data[k]),
                .o_ev_merged(ev_merged[k])
            );
        end
    endgenerate

    // Events of the PIO channels: producer i = channel i + 2
    always_comb begin
        for (int i = 0; i < 8; i++) begin
            o_ev_valid[i]          = ev_valid[i + 1];
            o_ev_type[8*i +: 8]    = ev_type[i + 1];
            o_ev_src[8*i +: 8]     = 8'(i + 2);
            o_ev_data[16*i +: 16]  = ev_data[i + 1];
            o_ev_merged[i]         = ev_merged[i + 1];
        end
    end

    // Output merge: whole blocks, round robin; TDEST = channel
    logic [NC*4-1:0] out_tdest;
    logic [NC*32-1:0] out_tdata_flat;
    always_comb begin
        for (int k = 0; k < NC; k++) begin
            out_tdest[4*k +: 4]       = 4'(k + 1);
            out_tdata_flat[32*k +: 32] = out_tdata[k];
        end
    end

    axis_arb_mux #(
        .S_COUNT(NC), .DATA_WIDTH(32), .KEEP_ENABLE(0), .ID_ENABLE(0), .DEST_ENABLE(1), .DEST_WIDTH(4),
        .USER_ENABLE(0), .LAST_ENABLE(1), .ARB_TYPE_ROUND_ROBIN(1), .ARB_LSB_HIGH_PRIORITY(1)
    ) u_out_mux (
        .clk(clk), .rst(!rst_n),
        .s_axis_tdata(out_tdata_flat), .s_axis_tkeep({NC*4{1'b1}}), .s_axis_tvalid(out_tvalid),
        .s_axis_tready(out_tready), .s_axis_tlast(out_tlast), .s_axis_tid({NC*8{1'b0}}),
        .s_axis_tdest(out_tdest), .s_axis_tuser({NC{1'b0}}),
        .m_axis_tdata(m_axis_tdata), .m_axis_tkeep(), .m_axis_tvalid(m_axis_tvalid),
        .m_axis_tready(m_axis_tready), .m_axis_tlast(m_axis_tlast), .m_axis_tid(), .m_axis_tdest(m_axis_tdest),
        .m_axis_tuser()
    );

    // ---------------------------------------------------------------- register reads
    wire [5:0] rblk = raddr[11:6];
    wire [5:0] roff = raddr[5:0];
    always_comb begin
        rdata = 32'd0;
        if (rblk == 6'd0 && roff == 6'h00) begin
            rdata = {8'd0, 8'(FRAME_WORDS), 16'(RING_WORDS)};          // DMA_CAPS
        end
        for (int k = 0; k < NC; k++) if (rblk == 6'(k + 1)) begin
            case (roff)
                6'h00: rdata = cfg[k];
                6'h04: rdata = {16'd0, count[k]};
                6'h08: rdata = {free[k], level[k]};
                6'h0C: rdata = {remaining[k], 14'd0, busy[k], i_done_flags[k]};
                6'h10: rdata = moved[k];
                6'h14: rdata = underruns[k];
                6'h18: rdata = overruns[k];
                6'h1C: rdata = triggers[k];
                6'h20: rdata = {8'd0, flush[k]};
                6'h24: rdata = {16'd0, thr[k]};
                default: rdata = 32'd0;
            endcase
        end
    end

    /* verilator lint_off UNUSEDSIGNAL */
    wire _unused = &{1'b0, rd, s_axis_tlast, ev_valid[0], ev_type[0], ev_data[0], ev_merged[0],
                     tx_tdata[0], tx_tvalid[0], in_tready[0]};
    /* verilator lint_on UNUSEDSIGNAL */
endmodule

`default_nettype wire
