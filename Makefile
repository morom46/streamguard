# StreamGuard - top level targets
#
#   make lint    Verilator lint of the RTL
#   make test    run the cocotb regression
#   make synth   generic synthesis with Yosys, prints a cell report
#   make clean

RTL_SRCS := rtl/sg_pkg.sv \
            rtl/sg_valid_stable.sv \
            rtl/sg_apb_regs.sv \
            rtl/sg_memrd.sv \
            rtl/sg_ptw.sv \
            rtl/sg_xlate.sv \
            rtl/sg_addr_chan.sv \
            rtl/sg_tbu.sv \
            rtl/streamguard.sv

LINT_FLAGS := -Wall -Wno-UNUSEDSIGNAL -Wno-UNUSEDPARAM -Wno-SYNCASYNCNET

.PHONY: lint test synth clean

lint:
	verilator --lint-only $(LINT_FLAGS) --assert -DSG_ASSERT \
	    --top-module streamguard $(RTL_SRCS)

test:
	$(MAKE) -C tb

synth:
	yowasp-yosys -q -l build/synth.log -p "\
	    read_verilog -sv $(filter-out rtl/sg_valid_stable.sv,$(RTL_SRCS)); \
	    hierarchy -top streamguard; synth -flatten; \
	    tee -o build/synth_stat.txt stat"
	@cat build/synth_stat.txt

build:
	mkdir -p build
synth: | build

clean:
	rm -rf build tb/sim_build tb/results.xml tb/__pycache__ tb/*.fst
