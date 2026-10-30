# pio2_core sources, paths relative to the repository root (one list for cocotb, the debugger
# build, formal and synthesis). Forencich verilog-axi and verilog-axis both define `arbiter` and
# `priority_encoder`; the two copies behave the same (they differ only in how the reset branch is
# written), so the verilog-axis copies serve both.
third_party/verilog-axis/rtl/axis_fifo.v
third_party/verilog-axis/rtl/axis_adapter.v
third_party/verilog-axis/rtl/axis_arb_mux.v
third_party/verilog-axis/rtl/arbiter.v
third_party/verilog-axis/rtl/priority_encoder.v
third_party/verilog-axi/rtl/axil_crossbar.v
third_party/verilog-axi/rtl/axil_crossbar_addr.v
third_party/verilog-axi/rtl/axil_crossbar_rd.v
third_party/verilog-axi/rtl/axil_crossbar_wr.v
third_party/verilog-axi/rtl/axil_register_rd.v
third_party/verilog-axi/rtl/axil_register_wr.v
src/rtl/asp_pio_sm.sv
src/rtl/asp_pio_tile.sv
src/rtl/axi/pio2_axil_regs.sv
src/rtl/dma/pio2_dma_chan.sv
src/rtl/dma/pio2_dma.sv
src/rtl/trace/pio2_event_fifo.sv
src/rtl/trace/pio2_trace_tap.sv
src/rtl/pio2_core_regs.sv
src/rtl/pio2_core.sv
