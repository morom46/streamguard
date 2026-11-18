# StreamGuard - top level targets
#
#   make lint    Verilator lint of the RTL
#   make test    run the cocotb regression (writes build/coverage.md)
#   make synth   generic synthesis with Yosys, prints a cell report
#   make formal  prove the properties in formal/ (SymbiYosys with Z3)
#   make clean

RTL_SRCS := rtl/sg_pkg.sv \
            rtl/sg_valid_stable.sv \
            rtl/sg_perm.sv \
            rtl/sg_tagcache.sv \
            rtl/sg_tlb.sv \
            rtl/sg_apb_regs.sv \
            rtl/sg_memrd.sv \
            rtl/sg_cmdq.sv \
            rtl/sg_evtq.sv \
            rtl/sg_ptw.sv \
            rtl/sg_xlate.sv \
            rtl/sg_addr_chan.sv \
            rtl/sg_tbu.sv \
            rtl/streamguard.sv

LINT_FLAGS := -Wall -Wno-UNUSEDSIGNAL -Wno-UNUSEDPARAM -Wno-SYNCASYNCNET

.PHONY: lint test synth formal clean

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

FORMAL_PROOFS := sg_perm sg_tagcache sg_tlb sg_addr_chan sg_memrd sg_evtq
SBY := yowasp-sby --yosys yowasp-yosys --smtbmc yowasp-yosys-smtbmc

formal: | build
	@for p in $(FORMAL_PROOFS); do \
	    if $(SBY) -f -d build/formal/$$p formal/$$p.sby > build/formal_$$p.log 2>&1; \
	    then echo "PROVED  $$p"; \
	    else echo "FAILED  $$p   (see build/formal_$$p.log)"; exit 1; fi; \
	done

clean:
	rm -rf build tb/sim_build tb/results.xml tb/__pycache__ tb/*.fst
