# cocotb regression for StreamGuard (Verilator)
#
#   make                      run everything
#   make COCOTB_TEST_MODULES=test_regs
#   make WAVES=1              also dump tb/dump.fst

SIM              ?= verilator
TOPLEVEL_LANG    ?= verilog
COCOTB_TOPLEVEL  ?= streamguard
COCOTB_TEST_MODULES ?= test_regs,test_bypass,test_translate

RTL := $(abspath ../rtl)
VERILOG_SOURCES := $(RTL)/sg_pkg.sv \
                   $(RTL)/sg_valid_stable.sv \
                   $(RTL)/sg_apb_regs.sv \
                   $(RTL)/sg_memrd.sv \
                   $(RTL)/sg_ptw.sv \
                   $(RTL)/sg_xlate.sv \
                   $(RTL)/sg_addr_chan.sv \
                   $(RTL)/sg_tbu.sv \
                   $(RTL)/streamguard.sv

# Assertions on, lint warnings do not stop the build (make lint does that)
EXTRA_ARGS += --assert -DSG_ASSERT -Wno-fatal

ifeq ($(WAVES),1)
EXTRA_ARGS += --trace-fst --trace-structs
endif

# The PyPI Verilator build lives inside its Python package.
VERILATOR_PKG := $(shell python3 -c "import verilator, os; print(os.path.dirname(verilator.__file__))" 2>/dev/null)
ifneq ($(VERILATOR_PKG),)
export VERILATOR_ROOT := $(VERILATOR_PKG)
endif

include $(shell cocotb-config --makefiles)/Makefile.sim
