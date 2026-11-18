# cocotb regression for StreamGuard (Verilator)
#
#   make                      run everything
#   make COCOTB_TEST_MODULES=test_regs
#   make WAVES=1              also dump tb/dump.fst
#   make PARAMS="TLB_POLICY=1"   build with other design parameters

SIM              ?= verilator
TOPLEVEL_LANG    ?= verilog
COCOTB_TOPLEVEL  ?= streamguard
# every tb/test_*.py file is a test module; coverage_report runs last and
# checks the functional coverage collected by all of them
comma := ,
empty :=
space := $(empty) $(empty)
COCOTB_TEST_MODULES ?= $(subst $(space),$(comma),$(sort $(basename $(notdir $(wildcard $(CURDIR)/test_*.py))))),coverage_report

# Design parameters to override, as NAME=VALUE pairs. Each set gets its
# own build directory, and the tests can read the values (SG_PARAMS).
# The regression is written for the default cache sizes, with either
# TLB policy; validate_model accepts any sizes.
PARAMS ?=
export SG_PARAMS := $(strip $(PARAMS))
ifneq ($(SG_PARAMS),)
EXTRA_ARGS += $(addprefix -G,$(SG_PARAMS))
SIM_BUILD := sim_build_$(subst =,-,$(subst $(space),_,$(SG_PARAMS)))
endif

RTL := $(abspath ../rtl)
VERILOG_SOURCES := $(RTL)/sg_pkg.sv \
                   $(RTL)/sg_valid_stable.sv \
                   $(RTL)/sg_perm.sv \
                   $(RTL)/sg_tagcache.sv \
                   $(RTL)/sg_tlb.sv \
                   $(RTL)/sg_apb_regs.sv \
                   $(RTL)/sg_memrd.sv \
                   $(RTL)/sg_cmdq.sv \
                   $(RTL)/sg_evtq.sv \
                   $(RTL)/sg_ptw.sv \
                   $(RTL)/sg_xlate.sv \
                   $(RTL)/sg_addr_chan.sv \
                   $(RTL)/sg_tbu.sv \
                   $(RTL)/streamguard.sv

# Assertions on, lint warnings do not stop the build (make lint does that)
EXTRA_ARGS += --assert -DSG_ASSERT -Wno-fatal

# Compile the model as a single C++ file. Above a certain design size
# Verilator splits it and uses a precompiled header, which the PyPI
# build of Verilator does not set up correctly.
EXTRA_ARGS += --output-split 0

ifeq ($(WAVES),1)
EXTRA_ARGS += --trace-fst --trace-structs
endif

# The PyPI Verilator build lives inside its Python package.
VERILATOR_PKG := $(shell python3 -c "import verilator, os; print(os.path.dirname(verilator.__file__))" 2>/dev/null)
ifneq ($(VERILATOR_PKG),)
export VERILATOR_ROOT := $(VERILATOR_PKG)
endif

include $(shell cocotb-config --makefiles)/Makefile.sim
