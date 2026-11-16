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
| 1 | APB register block, AXI4 bypass path, terminate with SLVERR | in progress |
| 2 | Hardware page table walker (AArch64, 4 KB granule) | planned |
| 3 | TLB and walk cache, stream table and context descriptor lookup | planned |
| 4 | Command queue (invalidation) and event queue (fault records) | planned |
| 5 | Reference model, functional coverage, formal properties | planned |
| 6 | TLB sizing study with synthesis area | planned |

## Tool setup

Needs Python 3.10+, `make`, `g++` and `perl`. On Ubuntu or WSL:

```sh
sudo apt install python3-venv build-essential perl
./scripts/setup_tools.sh
source .venv/bin/activate
```

## Layout

```
rtl/      SystemVerilog source
tb/       cocotb testbench (Python) and its Makefile
docs/     architecture and verification plan
scripts/  tool setup
```

## Documents

- [Architecture](docs/architecture.md): block diagram, interfaces, register map, design decisions
