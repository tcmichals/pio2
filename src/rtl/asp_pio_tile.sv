// Copyright (C) 2026 Tim Michals
// SPDX-License-Identifier: GPL-3.0-or-later

`default_nettype none

// pio2 PIO tile
//
// 1. Four state machines (SM0..SM3), RP2040 ISA.
// 2. Shared 32-word x 16-bit instruction memory.
// 3. Internal 8-bit IRQ bus (irq 0..7) for supervisor / worker chaining.
// 4. AXI4-Lite register port ([SPEC-AXI-04]).
// 5. Per SM an AXI-Stream TX input and RX output on its FIFOs, for the DMA engine ([SPEC-ICN-09]).
// 6. Read-only observation port for the trace tap ([SPEC-TRC-03]).
//
// @impl [SPEC-PIO-01] [SPEC-PIO-02] [SPEC-PIO-03] [SPEC-PIO-05] docs/SPECIFICATION.md
// @impl [SPEC-ICN-09] docs/INTERCONNECT_SPECIFICATION.md
module asp_pio_tile #(
    parameter logic [31:0] BASE_ADDR = 32'h40000700
) (
    input  wire          clk,
    input  wire          rst_n,

    // External Physical GPIO Pins (32-bit bus)
    input  wire [31:0]   i_gpio,
    output logic [31:0]  o_gpio_out,
    output logic [31:0]  o_gpio_pindir,

    // AXI4-Lite register slave ([SPEC-AXI-04]); same register map as the former Wishbone port
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

    // AXI-Stream FIFO ports, SM n in [32n+31:32n] / bit n ([SPEC-ICN-09]): TREADY = TX not full,
    // TVALID = RX not empty; held off in a clock where the register port uses the same FIFO
    input  wire  [127:0] s_axis_tx_tdata,
    input  wire  [3:0]   s_axis_tx_tvalid,
    output logic [3:0]   s_axis_tx_tready,
    output logic [127:0] m_axis_rx_tdata,
    output logic [3:0]   m_axis_rx_tvalid,
    input  wire  [3:0]   m_axis_rx_tready,

    // Hardware Tile Interrupt Request to CPU
    output logic         o_pio_irq,

    // Observation port for the trace tap ([SPEC-TRC-03]); read-only, changes nothing
    output logic [19:0]  o_obs_pc,           // SM n PC in [5n+4:5n]
    output logic [3:0]   o_obs_stalled,
    output logic [3:0]   o_obs_tx_empty,
    output logic [3:0]   o_obs_rx_full,
    output logic [3:0]   o_obs_active,
    output logic [7:0]   o_obs_irq
);

    // ------------------------------------------------------------------------
    // AXI4-Lite front end ([SPEC-AXI-04]). It turns each AXI4-Lite transaction into exactly one
    // request on the internal register port below (the former Wishbone slave logic, unchanged:
    // wb_* are now internal signals). One request at a time; reads and writes alternate when both
    // wait; a new request is taken only once the previous response has been accepted.
    // ------------------------------------------------------------------------
    // @impl [SPEC-AXI-04] docs/INTERCONNECT_SPECIFICATION.md
    logic        wb_cyc_i, wb_stb_i, wb_we_i;
    logic [31:0] wb_adr_i, wb_dat_i;
    logic [31:0] wb_dat_o;
    logic        wb_ack_o;

    logic        aw_held, w_held, ar_held;
    logic [31:0] aw_addr, w_data, ar_addr;
    logic [3:0]  w_strb;
    logic        req_busy, req_is_write, prefer_read;

    assign s_axil_awready = !aw_held;
    assign s_axil_wready  = !w_held;
    assign s_axil_arready = !ar_held;
    assign wb_cyc_i = req_busy;
    assign wb_stb_i = req_busy;
    wire wb_sel = wb_cyc_i && wb_stb_i;
    wire [11:0] reg_offset = wb_adr_i[11:0] - BASE_ADDR[11:0];

    wire write_ready = aw_held && w_held && !s_axil_bvalid;
    wire read_ready  = ar_held && !s_axil_rvalid;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            aw_held <= 1'b0; w_held <= 1'b0; ar_held <= 1'b0;
            aw_addr <= 32'd0; w_data <= 32'd0; w_strb <= 4'd0; ar_addr <= 32'd0;
            req_busy <= 1'b0; req_is_write <= 1'b0; prefer_read <= 1'b0;
            wb_we_i <= 1'b0; wb_adr_i <= 32'd0; wb_dat_i <= 32'd0;
            s_axil_bvalid <= 1'b0; s_axil_bresp <= 2'b00;
            s_axil_rvalid <= 1'b0; s_axil_rresp <= 2'b00; s_axil_rdata <= 32'd0;
        end else begin
            if (s_axil_awvalid && s_axil_awready) begin aw_held <= 1'b1; aw_addr <= s_axil_awaddr; end
            if (s_axil_wvalid && s_axil_wready) begin w_held <= 1'b1; w_data <= s_axil_wdata; w_strb <= s_axil_wstrb; end
            if (s_axil_arvalid && s_axil_arready) begin ar_held <= 1'b1; ar_addr <= s_axil_araddr; end
            if (s_axil_bvalid && s_axil_bready) s_axil_bvalid <= 1'b0;
            if (s_axil_rvalid && s_axil_rready) s_axil_rvalid <= 1'b0;

            if (req_busy) begin
                if (wb_ack_o) begin                     // the register block answered
                    req_busy <= 1'b0;
                    if (req_is_write) begin
                        aw_held <= 1'b0; w_held <= 1'b0;
                        s_axil_bresp <= 2'b00; s_axil_bvalid <= 1'b1;
                    end else begin
                        ar_held <= 1'b0;
                        s_axil_rdata <= wb_dat_o; s_axil_rresp <= 2'b00; s_axil_rvalid <= 1'b1;
                    end
                end
            end else if (write_ready && (!read_ready || !prefer_read)) begin
                prefer_read <= 1'b1;
                if (w_strb != 4'hF) begin               // partial write: no effect, SLVERR
                    aw_held <= 1'b0; w_held <= 1'b0;
                    s_axil_bresp <= 2'b10; s_axil_bvalid <= 1'b1;
                end else begin
                    req_busy <= 1'b1; req_is_write <= 1'b1;
                    wb_we_i <= 1'b1; wb_adr_i <= aw_addr; wb_dat_i <= w_data;
                end
            end else if (read_ready) begin
                prefer_read <= 1'b0;
                req_busy <= 1'b1; req_is_write <= 1'b0;
                wb_we_i <= 1'b0; wb_adr_i <= ar_addr;
            end
        end
    end

    // ------------------------------------------------------------------------
    // Shared Dual-Port Instruction Memory (32 Words x 16-Bit)
    // ------------------------------------------------------------------------
    logic [15:0] iram [0:31];
    logic [4:0]  sm_pc [0:3];
    logic [15:0] sm_instr [0:3];

    // Read ports for the 4 state machines
    always_comb begin
        for (int s = 0; s < 4; s++) begin
            sm_instr[s] = iram[sm_pc[s]];
        end
    end

    // Instruction memory in LUT RAM, no reset (the host loads it): one write port, one read
    // port per SM plus the bus read, so synthesis maps copies of RAM16SDP4 (EL-1027-41)
    wire        iram_we    = wb_cyc_i && wb_stb_i && !wb_ack_o && wb_we_i &&
                             (wb_adr_i[11:0] - BASE_ADDR[11:0]) >= 12'h010 &&
                             (wb_adr_i[11:0] - BASE_ADDR[11:0]) <= 12'h08C;
    wire [11:0] iram_off   = (wb_adr_i[11:0] - BASE_ADDR[11:0]) - 12'h010;
    wire [4:0]  iram_waddr = iram_off[6:2];
    /* verilator lint_off UNUSEDSIGNAL */
    wire        _unused_iram_off = |{iram_off[11:7], iram_off[1:0]};
    /* verilator lint_on UNUSEDSIGNAL */

    always_ff @(posedge clk) begin
        if (iram_we) iram[iram_waddr] <= wb_dat_i[15:0];
    end

    // ------------------------------------------------------------------------
    // Shared Internal IRQ Bus (irq 0..7)
    // ------------------------------------------------------------------------
    logic [7:0] irq_flags;
    logic [7:0] sm_irq_set [0:3];
    logic [7:0] sm_irq_clr [0:3];
    logic [7:0] combined_irq_set;
    logic [7:0] combined_irq_clr;
    logic [7:0] wb_irq_clr;

    always_comb begin
        combined_irq_set = sm_irq_set[0] | sm_irq_set[1] | sm_irq_set[2] | sm_irq_set[3];
        combined_irq_clr = sm_irq_clr[0] | sm_irq_clr[1] | sm_irq_clr[2] | sm_irq_clr[3] | wb_irq_clr;
    end

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            irq_flags <= 8'd0;
        end else begin
            irq_flags <= (irq_flags | combined_irq_set) & ~combined_irq_clr;
        end
    end

    assign o_pio_irq = |irq_flags[3:0]; // Lower 4 IRQs route to host interrupt

    // What the SMs see: a set or clear is visible one clock after the instruction ([SPEC-PIO-02] rule 6)
    logic [7:0] irq_view;
    assign irq_view = (irq_flags | combined_irq_set) & ~combined_irq_clr;
    // @impl [SPEC-TRC-03] docs/INTERCONNECT_SPECIFICATION.md
    assign o_obs_irq     = irq_flags;

    // ------------------------------------------------------------------------
    // Tile-Level Configuration & Status Registers
    // ------------------------------------------------------------------------
    logic [3:0] sm_en;
    logic [3:0] sm_restart;
    logic [3:0] sm_clkdiv_restart;

    logic [23:0] sm_clkdiv    [0:3];
    logic [31:0] sm_execctrl  [0:3];
    logic [31:0] sm_shiftctrl [0:3];
    logic [31:0] sm_pinctrl   [0:3];

    // FIFO interface per state machine: the register port and the stream ports share each FIFO
    logic [31:0] wb_tx_wdata  [0:3];
    logic [3:0]  wb_tx_wvalid;
    logic [31:0] sm_tx_wdata  [0:3];
    logic [3:0]  sm_tx_wvalid;
    logic [3:0]  sm_tx_ready;

    logic [31:0] sm_rx_rdata  [0:3];
    logic [3:0]  sm_rx_rvalid;
    logic [3:0]  wb_rx_ren;
    logic [3:0]  sm_rx_ren;

    // @impl [SPEC-ICN-09] docs/INTERCONNECT_SPECIFICATION.md
    // wb_tx_wvalid / wb_rx_ren are the register port's FIFO strobes in this clock; the stream port
    // is held off in that clock, so each word is taken or delivered exactly once. An RXF read pops in
    // the clock its data is captured (wb_rx_ren is combinational): with a registered pop one clock
    // later, the stream could take the captured word in between and the pop would drop the next one.
    always_comb begin
        for (int s = 0; s < 4; s++) begin
            wb_rx_ren[s] = wb_sel && !wb_ack_o && !wb_we_i && reg_offset == 12'(12'h114 + 12'h020 * s);
            s_axis_tx_tready[s]       = sm_tx_ready[s] && !wb_tx_wvalid[s];
            m_axis_rx_tvalid[s]       = sm_rx_rvalid[s] && !wb_rx_ren[s];
            m_axis_rx_tdata[32*s +: 32] = sm_rx_rdata[s];
            sm_tx_wvalid[s] = wb_tx_wvalid[s] | (s_axis_tx_tvalid[s] && s_axis_tx_tready[s]);
            sm_tx_wdata[s]  = wb_tx_wvalid[s] ? wb_tx_wdata[s] : s_axis_tx_tdata[32*s +: 32];
            sm_rx_ren[s]    = wb_rx_ren[s] | (m_axis_rx_tvalid[s] && m_axis_rx_tready[s]);
        end
    end

    // Pin writes per State Machine (one-clock strobes)
    logic [31:0] sm_out_wmask [0:3];
    logic [31:0] sm_out_wval  [0:3];
    logic [31:0] sm_dir_wmask [0:3];
    logic [31:0] sm_dir_wval  [0:3];

    // Status per State Machine
    logic [3:0]  sm_stalled;
    logic [3:0]  sm_exec_latched;
    logic [3:0]  sm_active;
    assign o_obs_stalled = sm_stalled;     // after the declarations (Icarus: no use before declaration)
    assign o_obs_active  = sm_active;

    // SMx_INSTR: immediate instruction per state machine
    logic [15:0] sm_exec_instr [0:3];
    logic [3:0]  sm_exec_valid;

    // Pin latches, one per pin for the whole tile ([SPEC-PIO-02] rule 10): each clock the write of
    // the highest-numbered SM writing a pin is applied; unwritten pins keep their value
    logic [31:0] out_q, dir_q;
    always_comb begin
        o_gpio_out    = out_q;
        o_gpio_pindir = dir_q;
        for (int s = 0; s < 4; s++) begin
            o_gpio_out    = (o_gpio_out    & ~sm_out_wmask[s]) | (sm_out_wval[s] & sm_out_wmask[s]);
            o_gpio_pindir = (o_gpio_pindir & ~sm_dir_wmask[s]) | (sm_dir_wval[s] & sm_dir_wmask[s]);
        end
    end
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            out_q <= '0;
            dir_q <= '0;                 // every pin an input after reset ([SPEC-PIO-09])
        end else begin
            out_q <= o_gpio_out;
            dir_q <= o_gpio_pindir;
        end
    end

    // ------------------------------------------------------------------------
    // Instantiate 4 State Machines (SM0 .. SM3)
    // ------------------------------------------------------------------------
    generate
        for (genvar s = 0; s < 4; s++) begin : gen_sm
            asp_pio_sm #(
                .SM_ID(s)
            ) u_sm (
                .clk              (clk),
                .rst_n            (rst_n),
                .i_en             (sm_en[s]),
                .i_restart        (sm_restart[s]),
                .i_clkdiv_restart (sm_clkdiv_restart[s]),
                .i_exec_instr     (sm_exec_instr[s]),
                .i_exec_valid     (sm_exec_valid[s]),
                .i_clkdiv         (sm_clkdiv[s]),
                .i_execctrl       (sm_execctrl[s]),
                .i_shiftctrl      (sm_shiftctrl[s]),
                .i_pinctrl        (sm_pinctrl[s]),
                .o_pc             (sm_pc[s]),
                .i_instr          (sm_instr[s]),
                .i_gpio           (i_gpio),
                .o_out_wmask      (sm_out_wmask[s]),
                .o_out_wval       (sm_out_wval[s]),
                .o_dir_wmask      (sm_dir_wmask[s]),
                .o_dir_wval       (sm_dir_wval[s]),
                .i_tx_wdata       (sm_tx_wdata[s]),
                .i_tx_wvalid      (sm_tx_wvalid[s]),
                .o_tx_ready       (sm_tx_ready[s]),
                .o_rx_rdata       (sm_rx_rdata[s]),
                .o_rx_rvalid      (sm_rx_rvalid[s]),
                .i_rx_ren         (sm_rx_ren[s]),
                .i_irq            (irq_view),
                .o_irq_set        (sm_irq_set[s]),
                .o_irq_clr        (sm_irq_clr[s]),
                .o_exec_stalled   (sm_stalled[s]),
                .o_exec_latched   (sm_exec_latched[s]),
                .o_sm_active      (sm_active[s]),
                .o_tx_empty       (o_obs_tx_empty[s]),
                .o_rx_full        (o_obs_rx_full[s])
            );
            assign o_obs_pc[5*s +: 5] = sm_pc[s];
        end
    endgenerate

    // ------------------------------------------------------------------------
    // Internal register port (the former Wishbone B4 slave logic; driven by the AXI4-Lite front end)
    // ------------------------------------------------------------------------
    /* verilator lint_off UNUSEDSIGNAL */
    wire _unused_addr = |wb_adr_i[31:12];
    /* verilator lint_on UNUSEDSIGNAL */

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            wb_ack_o            <= 1'b0;
            wb_dat_o            <= 32'd0;
            wb_irq_clr          <= 8'd0;
            sm_en               <= 4'd0;
            sm_restart          <= 4'd0;
            sm_clkdiv_restart   <= 4'd0;
            wb_tx_wvalid        <= 4'd0;
            sm_exec_valid       <= 4'd0;

            for (int s = 0; s < 4; s++) begin
                sm_clkdiv[s]    <= {16'd1, 8'd0}; // Div = 1.0 (full speed)
                sm_execctrl[s]  <= (32'd31 << 12) | (32'd0 << 7); // Wrap bottom=0, top=31
                sm_shiftctrl[s] <= 32'd0;
                sm_pinctrl[s]   <= 32'd0;
                wb_tx_wdata[s]  <= 32'd0;
                sm_exec_instr[s] <= 16'd0;
            end
        end else begin
            wb_ack_o         <= 1'b0;
            wb_irq_clr       <= 8'd0;
            sm_restart       <= 4'd0;
            sm_clkdiv_restart<= 4'd0;
            wb_tx_wvalid     <= 4'd0;
            sm_exec_valid    <= 4'd0;

            if (wb_sel && !wb_ack_o) begin
                wb_ack_o <= 1'b1;

                if (wb_we_i) begin
                    // Write Transactions
                    case (reg_offset)
                        12'h000: begin // CTRL
                            sm_en             <= wb_dat_i[3:0];
                            sm_restart        <= wb_dat_i[7:4];
                            sm_clkdiv_restart <= wb_dat_i[11:8];
                        end
                        12'h004: begin // IRQ Clear
                            wb_irq_clr <= wb_dat_i[7:0];
                        end
                        default: begin
                            // Instruction Memory (0x010 .. 0x08C)
                            // (instruction memory writes: see the iram block below)

                            // State Machine Registers (0x100 + 0x20 * N)
                            for (int s = 0; s < 4; s++) begin
                                int base_sm;
                                base_sm = 32'h100 + s * 32'h020;
                                if (reg_offset == 12'(base_sm + 12'h00)) begin
                                    sm_clkdiv[s] <= {wb_dat_i[31:16], wb_dat_i[15:8]};  // RP2040 CLKDIV: INT[31:16], FRAC[15:8]
                                end else if (reg_offset == 12'(base_sm + 12'h04)) begin
                                    sm_execctrl[s] <= wb_dat_i;
                                end else if (reg_offset == 12'(base_sm + 12'h08)) begin
                                    sm_shiftctrl[s] <= wb_dat_i;
                                end else if (reg_offset == 12'(base_sm + 12'h0C)) begin
                                    sm_pinctrl[s] <= wb_dat_i;
                                end else if (reg_offset == 12'(base_sm + 12'h10)) begin
                                    // TX FIFO Write
                                    wb_tx_wdata[s]  <= wb_dat_i;
                                    wb_tx_wvalid[s] <= 1'b1;
                                end else if (reg_offset == 12'(base_sm + 12'h18)) begin
                                    // SMx_INSTR: execute immediately ([SPEC-PIO-05])
                                    sm_exec_instr[s] <= wb_dat_i[15:0];
                                    sm_exec_valid[s] <= 1'b1;
                                end
                            end
                        end
                    endcase
                end else begin
                    // Read Transactions
                    case (reg_offset)
                        12'h000: wb_dat_o <= {28'd0, sm_en};
                        12'h004: wb_dat_o <= {24'd0, irq_flags};
                        12'h008: begin // FIFO Status
                            wb_dat_o <= {
                                ~sm_rx_rvalid, // RX empty
                                ~sm_tx_ready,  // TX full
                                24'd0
                            };
                        end
                        default: begin
                            // Instruction Memory Read
                            if (reg_offset >= 12'h010 && reg_offset <= 12'h08C) begin
                                int r_idx;
                                r_idx = (int'(reg_offset) - 32'h010) >> 2;
                                if (r_idx >= 0 && r_idx < 32) begin
                                    wb_dat_o <= {16'd0, iram[r_idx]};
                                end else begin
                                    wb_dat_o <= 32'd0;
                                end
                            end else begin
                                // State Machine Registers Read
                                wb_dat_o <= 32'd0;
                                for (int s = 0; s < 4; s++) begin
                                    int base_sm;
                                    base_sm = 32'h100 + s * 32'h020;
                                    if (reg_offset == 12'(base_sm + 12'h00)) begin
                                        wb_dat_o <= {sm_clkdiv[s][23:8], sm_clkdiv[s][7:0], 8'd0};
                                    end else if (reg_offset == 12'(base_sm + 12'h04)) begin
                                        wb_dat_o <= {sm_exec_latched[s], sm_execctrl[s][30:0]};  // EXEC_STALLED [31]
                                    end else if (reg_offset == 12'(base_sm + 12'h08)) begin
                                        wb_dat_o <= sm_shiftctrl[s];
                                    end else if (reg_offset == 12'(base_sm + 12'h0C)) begin
                                        wb_dat_o <= sm_pinctrl[s];
                                    end else if (reg_offset == 12'(base_sm + 12'h14)) begin
                                        // RX FIFO Read
                                        wb_dat_o     <= sm_rx_rdata[s];   // popped now (wb_rx_ren)
                                    end else if (reg_offset == 12'(base_sm + 12'h18)) begin
                                        // Status
                                        wb_dat_o <= {25'd0, sm_active[s], sm_stalled[s], sm_pc[s]};
                                    end
                                end
                            end
                        end
                    endcase
                end
            end
        end
    end

endmodule

`default_nettype wire
