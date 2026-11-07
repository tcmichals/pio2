// Copyright (C) 2026 Tim Michals
// SPDX-License-Identifier: GPL-3.0-or-later

`default_nettype none

// RP2040-compatible PIO state machine: runs pioasm output unchanged ([SPEC-PIO-02] conformance list).
//
// Written for FPGA area ([SPEC-PIO-01]): one always_comb computes the next state from one decode,
// one always_ff commits it. A PIO instruction does one shift, so IN and OUT share one rotator;
// pin masks are computed, not rotated; WAIT and JMP PIN share one pin mux; the FIFOs are RAM.
// NUM_PINS (8, 16 or 32) sets the pin rotators' size; pin numbers wrap at NUM_PINS.
//
// @impl [SPEC-PIO-01] [SPEC-PIO-02] [SPEC-PIO-03] [SPEC-PIO-15] docs/SPECIFICATION.md
module asp_pio_sm #(
    parameter int SM_ID = 0,
    parameter int NUM_PINS = 32
) (
    input  wire         clk,
    input  wire         rst_n,

    // Enable and Control Signals
    input  wire         i_en,
    input  wire         i_restart,
    input  wire         i_clkdiv_restart,

    // Immediate instruction (SMx_INSTR write): runs next cycle, even when disabled
    input  wire  [15:0] i_exec_instr,
    input  wire         i_exec_valid,

    // Configuration Registers
    /* verilator lint_off UNUSEDSIGNAL */
    input  wire [23:0]  i_clkdiv,       // [23:8] Int, [7:0] Frac
    input  wire [31:0]  i_execctrl,
    input  wire [31:0]  i_shiftctrl,
    input  wire [31:0]  i_pinctrl,
    /* verilator lint_on UNUSEDSIGNAL */

    // Instruction Memory Interface
    output logic [4:0]  o_pc,
    input  wire  [15:0] i_instr,

    // Physical GPIO Bus (All 32 pins)
    input  wire  [31:0] i_gpio,
    // Pin writes of this clock, one-clock strobes; the tile keeps the latches ([SPEC-PIO-02] rule 10)
    output logic [31:0] o_out_wmask,
    output logic [31:0] o_out_wval,
    output logic [31:0] o_dir_wmask,
    output logic [31:0] o_dir_wval,

    // TX FIFO Interface (Host / TLP ingress write -> SM read)
    input  wire  [31:0] i_tx_wdata,
    input  wire         i_tx_wvalid,
    output logic        o_tx_ready,

    // RX FIFO Interface (SM write -> Host / TLP Shim read)
    output logic [31:0] o_rx_rdata,
    output logic        o_rx_rvalid,
    input  wire         i_rx_ren,

    // Inter-SM IRQ Bus
    input  wire  [7:0]  i_irq,
    output logic [7:0]  o_irq_set,
    output logic [7:0]  o_irq_clr,

    // Observability / Status
    output logic        o_exec_stalled,
    output logic        o_exec_latched,     // EXECCTRL.EXEC_STALLED: an SMx_INSTR waits
    output logic        o_sm_active,
    output logic        o_tx_empty,         // observation for the trace tap ([SPEC-TRC-03])
    output logic        o_rx_full
);

    // ------------------------------------------------------------------------
    // Configuration fields (RP2040 register layout)
    // ------------------------------------------------------------------------
    wire [3:0] status_n        = i_execctrl[3:0];
    wire       status_sel      = i_execctrl[4];
    wire [4:0] wrap_bottom     = i_execctrl[11:7];
    wire [4:0] wrap_top        = i_execctrl[16:12];
    wire [4:0] jmp_pin         = i_execctrl[28:24];
    wire       sideset_pindirs = i_execctrl[29];
    wire       sideset_opt     = i_execctrl[30];

    wire       autopush        = i_shiftctrl[16];
    wire       autopull        = i_shiftctrl[17];
    wire       in_shift_right  = i_shiftctrl[18];
    wire       out_shift_right = i_shiftctrl[19];
    wire [5:0] push_thresh     = (i_shiftctrl[24:20] == 5'd0) ? 6'd32 : {1'b0, i_shiftctrl[24:20]};
    wire [5:0] pull_thresh     = (i_shiftctrl[29:25] == 5'd0) ? 6'd32 : {1'b0, i_shiftctrl[29:25]};
    wire       join_tx         = i_shiftctrl[30];   // FJOIN_TX: 8-deep TX, no RX
    wire       join_rx         = i_shiftctrl[31] && !join_tx;
    wire [3:0] tx_cap          = join_tx ? 4'd8 : (join_rx ? 4'd0 : 4'd4);
    wire [3:0] rx_cap          = join_rx ? 4'd8 : (join_tx ? 4'd0 : 4'd4);

    wire [4:0] in_base         = i_pinctrl[19:15];
    wire [4:0] out_base        = i_pinctrl[4:0];
    wire [4:0] set_base        = i_pinctrl[9:5];
    wire [4:0] side_base       = i_pinctrl[14:10];
    wire [5:0] out_count       = i_pinctrl[25:20];
    wire [2:0] set_count       = i_pinctrl[28:26];
    wire [2:0] sideset_count   = i_pinctrl[31:29];  // includes the enable bit when SIDE_EN

    // ------------------------------------------------------------------------
    // Clock divider (16.8 fractional)
    // ------------------------------------------------------------------------
    logic [15:0] clkdiv_int_cnt;
    logic [7:0]  clkdiv_frac_acc;
    logic        sm_clk_enable;

    wire [15:0] div_int  = i_clkdiv[23:8];
    wire [7:0]  div_frac = i_clkdiv[7:0];

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            clkdiv_int_cnt  <= 16'd0;
            clkdiv_frac_acc <= 8'd0;
            sm_clk_enable   <= 1'b0;
        end else if (i_clkdiv_restart || !i_en) begin
            clkdiv_int_cnt  <= 16'd0;
            clkdiv_frac_acc <= 8'd0;
            sm_clk_enable   <= 1'b0;
        end else if (div_int <= 16'd1 && div_frac == 8'd0) begin
            sm_clk_enable   <= 1'b1;  // divider 0 or 1.0: every clock
            clkdiv_int_cnt  <= 16'd0;
        end else begin
            if (clkdiv_int_cnt == 16'd0) begin
                logic [8:0] next_frac;
                next_frac = {1'b0, clkdiv_frac_acc} + {1'b0, div_frac};
                clkdiv_frac_acc <= next_frac[7:0];
                clkdiv_int_cnt  <= div_int + (next_frac[8] ? 16'd1 : 16'd0) - 16'd1;
                sm_clk_enable   <= 1'b1;
            end else begin
                clkdiv_int_cnt <= clkdiv_int_cnt - 16'd1;
                sm_clk_enable  <= 1'b0;
            end
        end
    end

    localparam int PB = $clog2(NUM_PINS);

    // ------------------------------------------------------------------------
    // State
    // ------------------------------------------------------------------------
    logic [4:0]  pc;
    logic [31:0] reg_x, reg_y, isr, osr;
    logic [5:0]  isr_count, osr_count;   // osr_count 32 = empty
    logic [4:0]  delay_cycles;
    logic        exec_pending;           // EXEC (OUT/MOV EXEC or SMx_INSTR) runs next
    logic [15:0] exec_instr;
    logic        exec_forced;            // SMx_INSTR: run even when disabled
    logic        push_pending;           // autopush waiting for RX FIFO space
    logic [4:0]  pend_delay;             // delay of the IN whose autopush is pending
    logic        irq_waiting;            // `irq wait` set its flag, waiting for clear
    logic        stalled;

    // One RAM per direction, 4 words or 8 when joined ([SPEC-PIO-02] rule 8). No reset: a word is
    // only read after it was written.
    logic [31:0] tx_mem [0:7];
    logic [31:0] rx_mem [0:7];
    logic [2:0]  tx_wr_ptr, tx_rd_ptr;
    logic [3:0]  tx_level;
    logic [2:0]  rx_wr_ptr, rx_rd_ptr;
    logic [3:0]  rx_level;
    logic [1:0]  join_q;

    wire [2:0] tx_rd_idx = join_tx ? tx_rd_ptr : {1'b0, tx_rd_ptr[1:0]};
    wire [2:0] tx_wr_idx = join_tx ? tx_wr_ptr : {1'b0, tx_wr_ptr[1:0]};
    wire [2:0] rx_rd_idx = join_rx ? rx_rd_ptr : {1'b0, rx_rd_ptr[1:0]};
    wire [2:0] rx_wr_idx = join_rx ? rx_wr_ptr : {1'b0, rx_wr_ptr[1:0]};

    assign o_pc           = pc;
    assign o_sm_active    = i_en;
    assign o_exec_stalled = stalled;
    assign o_exec_latched = exec_pending && exec_forced;
    assign o_tx_ready     = tx_level != tx_cap;
    assign o_rx_rvalid    = rx_level != 4'd0;
    assign o_tx_empty     = tx_level == 4'd0;
    assign o_rx_full      = rx_level == rx_cap && rx_cap != 4'd0;
    assign o_rx_rdata     = rx_mem[rx_rd_idx];
    wire [31:0] tx_head   = tx_mem[tx_rd_idx];

    function automatic logic [31:0] bit_reverse(input logic [31:0] v);
        logic [31:0] r;
        for (int i = 0; i < 32; i++) r[i] = v[31 - i];
        bit_reverse = r;
    endfunction

    // 32-bit rotate right, one stage per amount bit
    function automatic logic [31:0] rotr32(input logic [31:0] v, input logic [4:0] n);
        logic [31:0] r;
        r = v;
        if (n[0]) r = {r[0],    r[31:1]};
        if (n[1]) r = {r[1:0],  r[31:2]};
        if (n[2]) r = {r[3:0],  r[31:4]};
        if (n[3]) r = {r[7:0],  r[31:8]};
        if (n[4]) r = {r[15:0], r[31:16]};
        rotr32 = r;
    endfunction

    // Rotate right within NUM_PINS bits (the rest reads 0)
    function automatic logic [31:0] rotr_pins(input logic [31:0] v, input logic [4:0] n);
        logic [31:0] r, s;
        r = v;
        for (int k = 0; k < PB; k++) begin
            s = '0;
            for (int i = 0; i < NUM_PINS; i++) s[i] = n[k] ? r[(i + (1 << k)) % NUM_PINS] : r[i];
            r = s;
        end
        rotr_pins = r;
    endfunction

    function automatic logic [31:0] rotl_pins(input logic [31:0] v, input logic [4:0] n);
        rotl_pins = rotr_pins(v, 5'(0) - n);
    endfunction

    // n = 0..32 low bits set. Bit i is n > i, written as gates: i is constant, so each bit folds to a
    // little AND/OR logic, where a `>` would become a carry chain per bit in synthesis.
    function automatic logic [31:0] low_mask(input logic [5:0] n);
        logic [31:0] m;
        logic        gt;
        for (int i = 0; i < 32; i++) begin
            gt = 1'b0;
            for (int b = 0; b < 6; b++) gt = ((i >> b) & 1) != 0 ? (n[b] & gt) : (n[b] | gt);
            m[i] = gt;
        end
        low_mask = m;
    endfunction

    // The `count` pins from `base` upward, wrapping at NUM_PINS (no rotator: two thermometer codes)
    function automatic logic [31:0] pin_window(input logic [4:0] base, input logic [5:0] count);
        logic [PB:0]  b;
        logic [6:0]   sum;
        logic [31:0]  all_pins, m;
        b        = (PB + 1)'(base & 5'(NUM_PINS - 1));
        sum      = 7'(b) + 7'(count);
        all_pins = low_mask(6'(NUM_PINS));
        if (count >= 6'(NUM_PINS))         m = all_pins;
        else if (sum <= 7'(NUM_PINS))      m = low_mask(6'(sum)) & ~low_mask(6'(b));
        else                               m = (all_pins & ~low_mask(6'(b))) | low_mask(6'(sum - 7'(NUM_PINS)));
        pin_window = m;
    endfunction

    // RP2040 IRQ index: bit 4 = relative, {i[2], (i[1:0] + sm) % 4}; bit 3 is unused on RP2040
    /* verilator lint_off UNUSEDSIGNAL */
    function automatic logic [2:0] irq_index(input logic [4:0] f);
        logic [1:0] lo;
        lo = f[4] ? 2'(f[1:0] + 2'(SM_ID)) : f[1:0];
        irq_index = {f[2], lo};
    endfunction
    /* verilator lint_on UNUSEDSIGNAL */

    // ------------------------------------------------------------------------
    // Decode (once)
    // ------------------------------------------------------------------------
    wire [15:0] ins      = exec_pending ? exec_instr : i_instr;
    wire [2:0]  op       = ins[15:13];
    wire        op_in    = op == 3'b010;
    wire        is_exec  = (op == 3'b011 && ins[7:5] == 3'b111) || (op == 3'b101 && ins[7:5] == 3'b100);
    wire        forced   = exec_pending && exec_forced;
    wire        step     = forced || (i_en && sm_clk_enable);
    wire        flush    = {join_rx, join_tx} != join_q;  // changing FJOIN flushes both FIFOs (RP2040)
    wire [4:0]  pc_seq   = (pc == wrap_top) ? wrap_bottom : pc + 5'd1;

    wire [5:0]  shift_n  = (ins[4:0] == 5'd0) ? 6'd32 : {1'b0, ins[4:0]};
    wire [31:0] shift_lo = low_mask(shift_n);           // the n low bits
    wire [31:0] shift_hi = bit_reverse(shift_lo);       // the n high bits
    wire [31:0] pins_rot = rotr_pins(i_gpio, in_base);  // pin in_base is bit 0

    // One pin mux for WAIT and JMP PIN; pin numbers wrap at NUM_PINS
    wire [4:0]  pin_sel  = (op == 3'b001 && ins[6:5] == 2'b00) ? ins[4:0] :
                           (op == 3'b001 && ins[6:5] == 2'b01) ? 5'(in_base + ins[4:0]) : jmp_pin;
    wire        pin_val  = i_gpio[pin_sel & 5'(NUM_PINS - 1)];

    wire [2:0]  irq_k    = irq_index(ins[4:0]);

    // Delay / side-set field: SIDESET_COUNT includes the enable bit when SIDE_EN
    wire [2:0]  side_bits = sideset_opt ? (sideset_count == 3'd0 ? 3'd0 : sideset_count - 3'd1) : sideset_count;
    wire        side_en   = (sideset_count != 3'd0) && (!sideset_opt || ins[12]);
    wire [4:0]  side_val  = 5'(ins[12:8] >> (3'd5 - sideset_count)) & 5'(low_mask(6'(side_bits)));
    wire [4:0]  delay     = ins[12:8] & 5'(low_mask(6'(3'd5 - sideset_count)));

    // ------------------------------------------------------------------------
    // Next state
    // ------------------------------------------------------------------------
    logic [3:0]  tx_lvl, rx_lvl;
    logic        do_pull, do_push, tx_we;
    logic [31:0] push_word;
    logic [4:0]  next_pc;
    logic        stall, pc_jump, hold_for_push, from_exec;
    logic        d_en, d_dir;            // one data pin write per instruction
    logic [31:0] d_val;
    logic [5:0]  d_cnt;
    logic [4:0]  d_base;
    logic [31:0] w_mask, w_val, n_out, n_out_en, n_dir, n_dir_en;
    logic [31:0] n_x, n_y, n_isr, n_osr, cur_osr, in_val, in_merged, rot_out;
    logic [5:0]  n_isr_cnt, n_osr_cnt, cur_osr_cnt;
    logic [31:0] mov_s, mov_r;
    logic        cond;

    logic [4:0]  pc_d, delay_d, pend_delay_d;
    logic [31:0] x_d, y_d, isr_d, osr_d;
    logic [5:0]  isr_count_d, osr_count_d;
    logic        exec_pending_d, exec_forced_d, push_pending_d, irq_waiting_d, stalled_d;
    logic [15:0] exec_instr_d;
    logic [7:0]  irq_set_d, irq_clr_d;
    logic [31:0] out_wmask_d, out_wval_d, dir_wmask_d, dir_wval_d;
    logic        rx_pop;

    always_comb begin
        // Host side: RX read frees a slot before the SM looks at the level
        rx_pop = i_rx_ren && rx_level != 4'd0;
        tx_lvl = tx_level;
        rx_lvl = rx_level - (rx_pop ? 4'd1 : 4'd0);
        do_pull = 1'b0; do_push = 1'b0; push_word = isr;

        pc_d = pc; delay_d = delay_cycles; pend_delay_d = pend_delay;
        x_d = reg_x; y_d = reg_y; isr_d = isr; osr_d = osr;
        isr_count_d = isr_count; osr_count_d = osr_count;
        exec_pending_d = exec_pending; exec_forced_d = exec_forced; exec_instr_d = exec_instr;
        push_pending_d = push_pending; irq_waiting_d = irq_waiting; stalled_d = stalled;
        irq_set_d = '0; irq_clr_d = '0;
        out_wmask_d = '0; out_wval_d = o_out_wval; dir_wmask_d = '0; dir_wval_d = o_dir_wval;

        if (i_exec_valid) begin
            exec_pending_d = 1'b1;
            exec_instr_d   = i_exec_instr;
            exec_forced_d  = 1'b1;
        end

        from_exec = exec_pending;
        d_en = 1'b0; d_dir = 1'b0; d_val = '0; d_cnt = 6'd0; d_base = 5'd0;
        next_pc = 5'd0; pc_jump = 1'b0; stall = 1'b0; hold_for_push = 1'b0;
        cond = 1'b0; mov_s = '0; mov_r = '0;
        w_mask = '0; w_val = '0; n_out = '0; n_out_en = '0; n_dir = '0; n_dir_en = '0;

        // Autopull refills an empty OSR in the background ([SPEC-PIO-02] rule 3)
        cur_osr = osr; cur_osr_cnt = osr_count;
        if (step && delay_cycles == 5'd0 && !push_pending || (forced && !push_pending)) begin
            if (autopull && osr_count >= pull_thresh && tx_lvl != 4'd0 &&
                !(op == 3'b100 && ins[7])) begin  // a PULL takes the word itself
                do_pull = 1'b1; cur_osr = tx_head; cur_osr_cnt = 6'd0;
            end
        end
        n_x = reg_x; n_y = reg_y; n_isr = isr; n_osr = cur_osr;
        n_isr_cnt = isr_count; n_osr_cnt = cur_osr_cnt;

        // The one rotator: IN merges its bits, OUT shifts the OSR. Right: amount n; left: 32 - n.
        case (ins[7:5])
            3'b000:  in_val = pins_rot;
            3'b001:  in_val = reg_x;
            3'b010:  in_val = reg_y;
            3'b110:  in_val = isr;
            3'b111:  in_val = cur_osr;
            default: in_val = 32'd0;
        endcase
        in_merged = (isr & ~shift_lo) | (in_val & shift_lo);
        if (op_in) rot_out = in_shift_right ? rotr32(in_merged, ins[4:0]) : rotr32(isr, 5'(0) - ins[4:0]);
        else       rot_out = out_shift_right ? rotr32(cur_osr, ins[4:0]) : rotr32(cur_osr, 5'(0) - ins[4:0]);

        if (step && delay_cycles != 5'd0 && !forced) begin
            delay_d = delay_cycles - 5'd1;
        end else if (step && push_pending) begin
            // Autopush stalled on a full RX FIFO: retry the push, not the IN
            if (rx_lvl != rx_cap) begin
                do_push = 1'b1; push_word = isr;
                isr_d = '0; isr_count_d = 6'd0;
                push_pending_d = 1'b0;
                stalled_d = 1'b0;
                delay_d = pend_delay;
                pc_d = pc_seq;
            end else begin
                stalled_d = 1'b1;
            end
        end else if (step) begin
            case (op)
                3'b000: begin  // JMP
                    case (ins[7:5])
                        3'b000: cond = 1'b1;
                        3'b001: cond = reg_x == 32'd0;
                        3'b010: begin cond = reg_x != 32'd0; n_x = reg_x - 32'd1; end
                        3'b011: cond = reg_y == 32'd0;
                        3'b100: begin cond = reg_y != 32'd0; n_y = reg_y - 32'd1; end
                        3'b101: cond = reg_x != reg_y;
                        3'b110: cond = pin_val;
                        default: cond = cur_osr_cnt < pull_thresh;  // !OSRE
                    endcase
                    if (cond) begin pc_jump = 1'b1; next_pc = ins[4:0]; end
                end

                3'b001: begin  // WAIT
                    cond = (ins[6:5] == 2'b10) ? i_irq[irq_k] : pin_val;
                    if (cond != ins[7]) begin
                        stall = 1'b1;
                    end else if (ins[6:5] == 2'b10 && ins[7]) begin
                        irq_clr_d[irq_k] = 1'b1;
                    end
                end

                3'b010: begin  // IN
                    n_isr = in_shift_right ? rot_out : (rot_out & ~shift_lo) | (in_val & shift_lo);
                    n_isr_cnt = (isr_count + shift_n > 6'd32) ? 6'd32 : isr_count + shift_n;
                    if (autopush && n_isr_cnt >= push_thresh) begin
                        if (rx_lvl != rx_cap) begin
                            do_push = 1'b1; push_word = n_isr;
                            n_isr = '0; n_isr_cnt = 6'd0;
                        end else begin
                            hold_for_push = 1'b1;  // the shift commits; only the push waits
                        end
                    end
                end

                3'b011: begin  // OUT
                    if (autopull && cur_osr_cnt >= pull_thresh) begin
                        stall = 1'b1;  // empty OSR and empty FIFO
                    end else begin
                        if (out_shift_right) begin
                            mov_r = cur_osr & shift_lo;
                            n_osr = rot_out & ~shift_hi;
                        end else begin
                            mov_r = rot_out & shift_lo;
                            n_osr = rot_out & ~shift_lo;
                        end
                        n_osr_cnt = (cur_osr_cnt + shift_n > 6'd32) ? 6'd32 : cur_osr_cnt + shift_n;
                        case (ins[7:5])
                            3'b000: begin d_en = 1'b1; d_val = mov_r; d_cnt = out_count; d_base = out_base; end
                            3'b001: n_x = mov_r;
                            3'b010: n_y = mov_r;
                            3'b100: begin d_en = 1'b1; d_dir = 1'b1; d_val = mov_r; d_cnt = out_count; d_base = out_base; end
                            3'b101: begin pc_jump = 1'b1; next_pc = mov_r[4:0]; end
                            3'b110: begin n_isr = mov_r; n_isr_cnt = shift_n; end
                            3'b111: begin exec_pending_d = 1'b1; exec_instr_d = mov_r[15:0]; end
                            default: ;
                        endcase
                    end
                end

                3'b100: begin
                    if (!ins[7]) begin  // PUSH: [6] iffull, [5] block
                        if (ins[6] && isr_count < push_thresh) begin
                            // below threshold: no-op
                        end else if (rx_lvl != rx_cap) begin
                            do_push = 1'b1; push_word = isr;
                            n_isr = '0; n_isr_cnt = 6'd0;
                        end else if (ins[5]) begin
                            stall = 1'b1;
                        end else begin
                            n_isr = '0; n_isr_cnt = 6'd0;  // noblock, full: dropped
                        end
                    end else begin      // PULL: [6] ifempty, [5] block
                        if (ins[6] && cur_osr_cnt < pull_thresh) begin
                            // not empty yet: no-op
                        end else if (tx_lvl != 4'd0) begin
                            do_pull = 1'b1; n_osr = tx_head; n_osr_cnt = 6'd0;
                        end else if (ins[5]) begin
                            stall = 1'b1;
                        end else begin
                            n_osr = reg_x; n_osr_cnt = 6'd0;  // noblock, empty: OSR = X
                        end
                    end
                end

                3'b101: begin  // MOV
                    case (ins[2:0])
                        3'b000:  mov_s = pins_rot;
                        3'b001:  mov_s = reg_x;
                        3'b010:  mov_s = reg_y;
                        3'b101:  mov_s = ((status_sel ? rx_lvl : tx_lvl) < status_n) ? 32'hFFFF_FFFF : 32'd0;
                        3'b110:  mov_s = isr;
                        3'b111:  mov_s = cur_osr;
                        default: mov_s = 32'd0;
                    endcase
                    case (ins[4:3])
                        2'b01:   mov_r = ~mov_s;
                        2'b10:   mov_r = bit_reverse(mov_s);
                        default: mov_r = mov_s;
                    endcase
                    case (ins[7:5])
                        3'b000: begin d_en = 1'b1; d_val = mov_r; d_cnt = out_count; d_base = out_base; end
                        3'b001: n_x = mov_r;
                        3'b010: n_y = mov_r;
                        3'b011: begin d_en = 1'b1; d_dir = 1'b1; d_val = mov_r; d_cnt = out_count; d_base = out_base; end  // PINDIRS (RP2350)
                        3'b100: begin exec_pending_d = 1'b1; exec_instr_d = mov_r[15:0]; end
                        3'b101: begin pc_jump = 1'b1; next_pc = mov_r[4:0]; end
                        3'b110: begin n_isr = mov_r; n_isr_cnt = 6'd0; end
                        default: begin n_osr = mov_r; n_osr_cnt = 6'd0; end
                    endcase
                end

                3'b110: begin  // IRQ: [6] clr, [5] wait, [4:0] index
                    if (ins[6]) begin
                        irq_clr_d[irq_k] = 1'b1;
                    end else if (!ins[5]) begin
                        irq_set_d[irq_k] = 1'b1;
                    end else if (!irq_waiting) begin
                        irq_set_d[irq_k] = 1'b1;   // set, then wait for someone to clear it
                        irq_waiting_d    = 1'b1;
                        stall = 1'b1;
                    end else if (i_irq[irq_k]) begin
                        stall = 1'b1;
                    end else begin
                        irq_waiting_d = 1'b0;
                    end
                end

                default: begin  // SET
                    case (ins[7:5])
                        3'b000: begin d_en = 1'b1; d_val = {27'd0, ins[4:0]}; d_cnt = {3'd0, set_count}; d_base = set_base; end
                        3'b001: n_x = {27'd0, ins[4:0]};
                        3'b010: n_y = {27'd0, ins[4:0]};
                        3'b100: begin d_en = 1'b1; d_dir = 1'b1; d_val = {27'd0, ins[4:0]}; d_cnt = {3'd0, set_count}; d_base = set_base; end
                        default: ;
                    endcase
                end
            endcase

            // Pin writers: data (only when the instruction completes), then side-set
            // (asserted even while stalled; wins on a shared pin, as on the RP2040)
            if (d_en && !stall) begin
                w_mask = pin_window(d_base, d_cnt);
                w_val  = rotl_pins(d_val, d_base);
                if (d_dir) begin
                    n_dir = w_val; n_dir_en = w_mask;
                end else begin
                    n_out = w_val; n_out_en = w_mask;
                end
            end
            if (side_en && side_bits != 3'd0) begin
                w_mask = pin_window(side_base, 6'(side_bits));
                w_val  = rotl_pins({27'd0, side_val}, side_base);
                if (sideset_pindirs) begin
                    n_dir = (n_dir & ~w_mask) | (w_val & w_mask); n_dir_en = n_dir_en | w_mask;
                end else begin
                    n_out = (n_out & ~w_mask) | (w_val & w_mask); n_out_en = n_out_en | w_mask;
                end
            end
            out_wmask_d = n_out_en; out_wval_d = n_out;
            dir_wmask_d = n_dir_en; dir_wval_d = n_dir;

            stalled_d = stall || hold_for_push;
            if (!stall) begin
                // Commit: a stalled instruction changes nothing except side-set
                x_d = n_x; y_d = n_y; isr_d = n_isr; osr_d = n_osr;
                isr_count_d = n_isr_cnt; osr_count_d = n_osr_cnt;
                if (from_exec) begin
                    exec_pending_d = is_exec;
                    exec_forced_d  = 1'b0;
                    if (pc_jump) pc_d = next_pc;  // an EXEC'd instruction moves the PC only by jumping
                end else if (hold_for_push) begin
                    push_pending_d = 1'b1;    // PC and delay wait for the push
                    pend_delay_d   = delay;
                end else begin
                    pc_d = pc_jump ? next_pc : pc_seq;
                end
                if (!is_exec && !hold_for_push) delay_d = delay;   // OUT/MOV EXEC ignore their own delay
            end else if (do_pull) begin
                osr_d = cur_osr; osr_count_d = cur_osr_cnt;  // background refill lands even when stalled
            end
        end

        // FIFO levels
        if (do_pull) tx_lvl = tx_lvl - 4'd1;
        if (do_push) rx_lvl = rx_lvl + 4'd1;
        tx_we = i_tx_wvalid && tx_lvl != tx_cap;
        if (tx_we) tx_lvl = tx_lvl + 4'd1;

        // SM_RESTART ([SPEC-PIO-02] rule 7): PC, OSR, X, Y, FIFOs and pins are kept
        if (i_restart) begin
            isr_d = '0; isr_count_d = 6'd0; osr_count_d = 6'd32;
            delay_d = 5'd0; exec_pending_d = 1'b0; exec_forced_d = 1'b0;
            push_pending_d = 1'b0; irq_waiting_d = 1'b0; stalled_d = 1'b0;
        end
    end

    // FIFO storage: one write and one read per RAM per clock
    always_ff @(posedge clk) begin
        if (!flush && do_push) rx_mem[rx_wr_idx] <= push_word;
        if (!flush && tx_we)   tx_mem[tx_wr_idx] <= i_tx_wdata;
    end

    // ------------------------------------------------------------------------
    // Commit
    // ------------------------------------------------------------------------
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            pc <= 5'd0; reg_x <= '0; reg_y <= '0; isr <= '0; osr <= '0;
            isr_count <= 6'd0; osr_count <= 6'd32; delay_cycles <= 5'd0;
            exec_pending <= 1'b0; exec_instr <= 16'd0; exec_forced <= 1'b0;
            push_pending <= 1'b0; pend_delay <= 5'd0; irq_waiting <= 1'b0; stalled <= 1'b0;
            tx_wr_ptr <= '0; tx_rd_ptr <= '0; tx_level <= '0;
            rx_wr_ptr <= '0; rx_rd_ptr <= '0; rx_level <= '0;
            o_out_wmask <= '0; o_out_wval <= '0; o_dir_wmask <= '0; o_dir_wval <= '0;
            o_irq_set <= '0; o_irq_clr <= '0;
            join_q <= 2'd0;
        end else if (flush) begin
            join_q <= {join_rx, join_tx};
            tx_wr_ptr <= '0; tx_rd_ptr <= '0; tx_level <= '0;
            rx_wr_ptr <= '0; rx_rd_ptr <= '0; rx_level <= '0;
        end else begin
            pc <= pc_d; delay_cycles <= delay_d; pend_delay <= pend_delay_d;
            reg_x <= x_d; reg_y <= y_d; isr <= isr_d; osr <= osr_d;
            isr_count <= isr_count_d; osr_count <= osr_count_d;
            exec_pending <= exec_pending_d; exec_forced <= exec_forced_d; exec_instr <= exec_instr_d;
            push_pending <= push_pending_d; irq_waiting <= irq_waiting_d; stalled <= stalled_d;
            o_irq_set <= irq_set_d; o_irq_clr <= irq_clr_d;
            o_out_wmask <= out_wmask_d; o_out_wval <= out_wval_d;
            o_dir_wmask <= dir_wmask_d; o_dir_wval <= dir_wval_d;
            if (rx_pop)  rx_rd_ptr <= rx_rd_ptr + 3'd1;
            if (do_pull) tx_rd_ptr <= tx_rd_ptr + 3'd1;
            if (do_push) rx_wr_ptr <= rx_wr_ptr + 3'd1;
            if (tx_we)   tx_wr_ptr <= tx_wr_ptr + 3'd1;
            tx_level <= tx_lvl;
            rx_level <= rx_lvl;
        end
    end

endmodule

`default_nettype wire
