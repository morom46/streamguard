"""Reference model of the StreamGuard translation.

A plain Python description of what the hardware must decide for one
access: forwarded to which address, or terminated with which fault.
It follows the same lookup order as the specification:

    StreamID range -> Stream Table Entry -> Context Descriptor -> table walk

It only reads memory through the `rd64` callable it is given, so the
same function works against the testbench memory or a dictionary.

The result also says whether the fault is recorded in the event queue:
configuration errors always are, a deliberate abort never is, and the
translation-related faults only when the context descriptor has CD.R
set.
"""

from dataclasses import dataclass

# Fault codes = SMMUv3 event numbers
FC_NONE = 0x00
FC_BAD_STREAMID = 0x02
FC_STE_FETCH = 0x03
FC_BAD_STE = 0x04
FC_CD_FETCH = 0x09
FC_BAD_CD = 0x0A
FC_WALK_EABT = 0x0B
FC_TRANSLATION = 0x10
FC_ADDR_SIZE = 0x11
FC_ACCESS = 0x12
FC_PERMISSION = 0x13

# Faults whose event record carries the input address and access type
XLAT_FAULTS = (FC_TRANSLATION, FC_ADDR_SIZE, FC_ACCESS, FC_PERMISSION, FC_WALK_EABT)

FC_NAMES = {
    FC_NONE: "none", FC_BAD_STREAMID: "C_BAD_STREAMID", FC_BAD_STE: "C_BAD_STE",
    FC_BAD_CD: "C_BAD_CD", FC_TRANSLATION: "F_TRANSLATION",
    FC_ADDR_SIZE: "F_ADDR_SIZE", FC_ACCESS: "F_ACCESS",
    FC_PERMISSION: "F_PERMISSION",
}

STE_CFG_ABORT, STE_CFG_BYPASS, STE_CFG_S1 = 0b000, 0b100, 0b101

PROT_PRIVILEGED = 0b001
PROT_INSTRUCTION = 0b100


@dataclass
class Result:
    fault: bool
    addr: int = 0          # output address when not a fault
    fcode: int = FC_NONE   # FC_NONE with fault=True: terminated, no event
    level: int = 0         # level of the fault, or of the leaf on success
    reads: int = 0         # table memory read requests the lookup needs
    event: bool = False    # the fault is recorded in the event queue

    def __str__(self):
        if not self.fault:
            return f"ok -> {self.addr:#x}"
        return f"fault {FC_NAMES.get(self.fcode, hex(self.fcode))} level {self.level}"


def bits(value, hi, lo):
    return (value >> lo) & ((1 << (hi - lo + 1)) - 1)


def start_level(t0sz):
    if t0sz <= 24:
        return 0
    if t0sz <= 33:
        return 1
    return 2


def walk(rd64, ttb, t0sz, affd, va, write, prot):
    """Stage 1 walk, 4 KB granule. Returns (fcode, level, pa, reads)."""
    level = start_level(t0sz)
    if va >> (64 - t0sz):
        return FC_TRANSLATION, level, 0, 0

    table = ttb & ~0xFFF & ((1 << 48) - 1)
    reads = 0
    while True:
        shift = 39 - 9 * level
        index = (va >> shift) & 0x1FF
        desc = rd64(table + index * 8)
        reads += 1

        if not desc & 1:
            return FC_TRANSLATION, level, 0, reads
        is_table_bit = bool(desc & 2)
        if is_table_bit and level < 3:
            kind = "table"
        elif is_table_bit and level == 3:
            kind = "leaf"
        elif not is_table_bit and level in (1, 2):
            kind = "leaf"
        else:
            return FC_TRANSLATION, level, 0, reads      # reserved encoding

        if bits(desc, 51, 48):
            return FC_ADDR_SIZE, level, 0, reads

        if kind == "table":
            table = bits(desc, 47, 12) << 12
            level += 1
            continue

        if not (desc >> 10) & 1 and not affd:
            return FC_ACCESS, level, 0, reads

        read_only = (desc >> 7) & 1
        el0_ok = (desc >> 6) & 1
        pxn = (desc >> 53) & 1
        uxn = (desc >> 54) & 1
        privileged = bool(prot & PROT_PRIVILEGED)
        instruction = bool(prot & PROT_INSTRUCTION)
        denied = False
        if write and read_only:
            denied = True
        if not privileged and not el0_ok:
            denied = True
        if instruction and not write:
            if privileged and pxn:
                denied = True
            if not privileged and uxn:
                denied = True
        if denied:
            return FC_PERMISSION, level, 0, reads

        offset_bits = shift
        pa = (bits(desc, 47, offset_bits) << offset_bits) | (va & ((1 << offset_bits) - 1))
        return FC_NONE, level, pa, reads


def translate(rd64, strtab_base, log2size, sid, va, write, prot,
              smmuen=True, gbpa_abort=False):
    if not smmuen:
        return Result(fault=gbpa_abort, addr=va)

    if sid >> log2size:
        return Result(True, fcode=FC_BAD_STREAMID, event=True)

    ste = rd64((strtab_base & ~0x3F) + sid * 64)
    cfg = bits(ste, 3, 1)
    if not ste & 1 or cfg not in (STE_CFG_ABORT, STE_CFG_BYPASS, STE_CFG_S1):
        return Result(True, fcode=FC_BAD_STE, reads=1, event=True)
    if cfg == STE_CFG_S1 and bits(ste, 5, 4) != 0:
        return Result(True, fcode=FC_BAD_STE, reads=1, event=True)
    if cfg == STE_CFG_ABORT:
        return Result(True, reads=1)
    if cfg == STE_CFG_BYPASS:
        return Result(False, addr=va, reads=1)

    cd_addr = bits(ste, 47, 6) << 6
    cd0 = rd64(cd_addr)
    cd1 = rd64(cd_addr + 8)
    t0sz = bits(cd0, 5, 0)
    valid = (cd0 >> 31) & 1
    aa64 = (cd0 >> 41) & 1
    endi = (cd0 >> 15) & 1
    tg0 = bits(cd0, 7, 6)
    if not valid or not aa64 or endi or tg0 != 0 or not 16 <= t0sz <= 39:
        return Result(True, fcode=FC_BAD_CD, reads=2, event=True)
    record = bool((cd0 >> 45) & 1)                    # CD.R
    if (cd0 >> 14) & 1:                               # EPD0
        return Result(True, fcode=FC_TRANSLATION, reads=2, event=record)

    affd = (cd0 >> 35) & 1
    fcode, level, pa, reads = walk(rd64, cd1, t0sz, affd, va, write, prot)
    if fcode != FC_NONE:
        return Result(True, fcode=fcode, level=level, reads=2 + reads, event=record)
    return Result(False, addr=pa, level=level, reads=2 + reads)
