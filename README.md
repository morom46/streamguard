# StreamGuard

A stream-aware I/O address translation unit in SystemVerilog.

StreamGuard sits on an AXI4 link between a device and the memory system and
decides, per transaction, whether the device's address is translated and
forwarded or the access is terminated. It is built from a subset of the public
Arm SMMUv3 architecture specification (IHI 0070): the same register map,
stream table, context descriptor and page table formats, cut down to stage 1
translation with 4 KB pages.

This is a learning project. It is not a compliant SMMUv3 implementation and
contains no Arm IP. What is and is not implemented is listed in
[docs/architecture.md](docs/architecture.md).

## Status

| Milestone | Content | State |
|---|---|---|
| 1 | APB register block, AXI4 bypass path, terminate with SLVERR | done |
| 2 | Stream table and context descriptor lookup, page table walker (AArch64, 4 KB granule, 2 MB and 1 GB blocks) | done |
| 3 | TLB, walk cache and configuration cache, invalidation, hit counters | done |
| 4 | Command queue (TLBI, CFGI, SYNC), event queue (fault records), GERROR, interrupts | done |
| 5 | Functional coverage (104 bins, closed), formal proofs of six blocks | done |
| 6 | Second TLB policy (NRU), TLB sizing study with a model checked against the RTL | done |

Regression: 73 tests, passing on 14 random seeds. Every translated access is
checked against a Python reference model, with the caches off and on, and the
event queue is checked after every access. Functional coverage is 104 of 104
bins. Six blocks have formal proofs (TLB, tag cache, permission check, address
channel, memory read port, event queue writer).
A TLB hit takes 4 cycles; a lookup that misses everything takes 44.

## Results

- **Sizing study** ([report](docs/sizing_study.md)): 240 cache configurations
  replayed over four device workloads, including an NPU running a
  convolution layer, with area from synthesis. Switching the TLB from round
  robin to not-recently-used cuts translation time by 9% at the same size,
  with no workload slower, for 5 flip-flops. Mapping the NPU's buffers with
  2 MB blocks does more than any TLB size.
- **Model checked against the RTL**: across 205,305 replayed accesses the
  study's cache model matches the hardware's hit counters, table reads and
  translation cycles exactly.
- **Verification**: 73 tests with a reference model, 104 of 104 functional
  coverage bins, 7 formal proofs, and every check shown to fail on
  deliberately injected bugs ([verification plan](docs/verification_plan.md)).

## Quick start

Needs Python 3.10+, `make`, `g++` and `perl`. On Ubuntu or WSL:

```sh
sudo apt install python3-venv build-essential perl
./scripts/setup_tools.sh
source .venv/bin/activate

make lint     # Verilator lint
make test     # cocotb regression, writes build/coverage.md
make formal   # formal proofs (SymbiYosys with Z3)
make synth    # Yosys generic synthesis, prints a cell report

make validate # check the sizing study's model against the RTL (about 4 minutes)
make study    # run the sizing study; synthesises every cache size the first time
```

Another build of the design: `make -C tb PARAMS="TLB_POLICY=1"`.

Waveforms: `make -C tb WAVES=1` writes `tb/dump.fst` (open with GTKWave or Surfer).
A fixed seed: `COCOTB_RANDOM_SEED=3 make test`.

## Layout

```
rtl/      SystemVerilog source
tb/       cocotb testbench (Python) and its Makefile
formal/   proof scripts; the properties are at the end of each rtl module
study/    sizing study: workloads, cache model, area, report generator
docs/     architecture and verification plan
scripts/  tool setup
```

## Documents

- [Architecture](docs/architecture.md): block diagram, interfaces, register map, design decisions
- [Verification plan](docs/verification_plan.md): what is tested, how, and current results
- [Sizing study](docs/sizing_study.md): how big the caches should be, and which TLB policy
