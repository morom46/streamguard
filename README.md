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
| 5 | Functional coverage, formal properties | planned |
| 6 | TLB sizing study with synthesis area | planned |

Regression: 72 tests, passing on 8 random seeds. Every translated access is
checked against a Python reference model, with the caches off and on, and the
event queue is checked after every access.
A TLB hit takes 4 cycles; a lookup that misses everything takes 44.

## Quick start

Needs Python 3.10+, `make`, `g++` and `perl`. On Ubuntu or WSL:

```sh
sudo apt install python3-venv build-essential perl
./scripts/setup_tools.sh
source .venv/bin/activate

make lint     # Verilator lint
make test     # cocotb regression
make synth    # Yosys generic synthesis, prints a cell report
```

Waveforms: `make -C tb WAVES=1` writes `tb/dump.fst` (open with GTKWave or Surfer).
A fixed seed: `COCOTB_RANDOM_SEED=3 make test`.

## Layout

```
rtl/      SystemVerilog source
tb/       cocotb testbench (Python) and its Makefile
docs/     architecture and verification plan
scripts/  tool setup
```

## Documents

- [Architecture](docs/architecture.md): block diagram, interfaces, register map, design decisions
- [Verification plan](docs/verification_plan.md): what is tested, how, and current results
