# StreamGuard - top level targets
#
#   make lint    Verilator lint of the RTL
#   make test    run the cocotb regression (writes build/coverage.md)
#   make synth   generic synthesis with Yosys, prints a cell report
#   make formal  prove the properties in formal/ (SymbiYosys with Z3)
#   make validate  check the sizing study's model against the RTL (slow)
#   make study   run the TLB sizing study, writes docs/sizing_study.md
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

.PHONY: lint test synth formal validate study clean

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

FORMAL_PROOFS := sg_perm sg_tagcache sg_tlb sg_tlb_nru sg_addr_chan sg_memrd sg_evtq
SBY := yowasp-sby --yosys yowasp-yosys --smtbmc yowasp-yosys-smtbmc

formal: | build
	@for p in $(FORMAL_PROOFS); do \
	    if $(SBY) -f -d build/formal/$$p formal/$$p.sby > build/formal_$$p.log 2>&1; \
	    then echo "PROVED  $$p"; \
	    else echo "FAILED  $$p   (see build/formal_$$p.log)"; exit 1; fi; \
	done

# Cache configurations the study's model is checked against
VALIDATE_CONFIGS := \
    "TLB_ENTRIES=8 WC_ENTRIES=4 CFG_ENTRIES=4 TLB_POLICY=0" \
    "TLB_ENTRIES=2 WC_ENTRIES=1 CFG_ENTRIES=1 TLB_POLICY=1" \
    "TLB_ENTRIES=16 WC_ENTRIES=8 CFG_ENTRIES=2 TLB_POLICY=1" \
    "TLB_ENTRIES=32 WC_ENTRIES=16 CFG_ENTRIES=8 TLB_POLICY=0"

validate:
	rm -rf build/validation
	@for p in $(VALIDATE_CONFIGS); do \
	    $(MAKE) -C tb COCOTB_TEST_MODULES=validate_model PARAMS="$$p" || exit 1; \
	done
	SG_READ_LATENCY=20 $(MAKE) -C tb COCOTB_TEST_MODULES=validate_model \
	    PARAMS="TLB_ENTRIES=16 WC_ENTRIES=8 CFG_ENTRIES=2 TLB_POLICY=1"

study:
	cd study && python run_study.py

clean:
	rm -rf build tb/sim_build* tb/results.xml tb/__pycache__ tb/*.fst
