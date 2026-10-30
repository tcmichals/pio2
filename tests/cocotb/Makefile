# tests/cocotb/Makefile: cocotb regression for the PIO tile (Verilator by default)
#   make TOPLEVEL=asp_pio_tile MODULE=test_asp_pio_cocotb
# Run through uv so cocotb and pio2 come from the project environment: see README.md.

SIM ?= verilator
TOPLEVEL_LANG ?= verilog
RTL_DIR = $(abspath $(CURDIR)/../../src/rtl)

TOPLEVEL ?= asp_pio_tile
MODULE ?= test_asp_pio_cocotb

# The repository root, for third_party/ (pio2-cocotb runs in a temporary copy and passes PIO2_ROOT).
PIO2_ROOT ?= $(abspath $(CURDIR)/../..)
AXIS_DIR = $(PIO2_ROOT)/third_party/verilog-axis/rtl

ifeq ($(TOPLEVEL),pio2_event_fifo)
VERILOG_SOURCES = $(AXIS_DIR)/axis_fifo.v $(RTL_DIR)/trace/pio2_event_fifo.sv
SIM_PARAMS ?= -GDEPTH=16
else
VERILOG_SOURCES = $(RTL_DIR)/asp_pio_sm.sv \
                  $(RTL_DIR)/asp_pio_tlp_ingress.sv \
                  $(RTL_DIR)/asp_pio_tlp_shim.sv \
                  $(RTL_DIR)/asp_pio_tile.sv
endif

# Same Verilator flags as the AbstractX regression, so results compare one to one.
SIM_PARAMS ?=
TRACE ?=
EXTRA_ARGS += $(TRACE) -I$(RTL_DIR) -Wno-WIDTHEXPAND -Wno-WIDTHTRUNC -Wno-SELRANGE -Wno-PINCONNECTEMPTY -Wno-UNUSEDPARAM $(SIM_PARAMS)

include $(shell cocotb-config --makefiles)/Makefile.sim
