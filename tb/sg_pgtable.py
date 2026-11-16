"""Builders for the in-memory structures StreamGuard reads:
stream table entries, context descriptors and translation tables."""

LEVEL_SHIFT = {0: 39, 1: 30, 2: 21, 3: 12}
PAGE, BLOCK_2M, BLOCK_1G = 3, 2, 1          # leaf level for each mapping size


def leaf_desc(pa, level, af=1, el0=1, ro=0, pxn=0, uxn=0):
    """Page (level 3) or block (level 1, 2) descriptor."""
    shift = LEVEL_SHIFT[level]
    desc = (pa >> shift << shift) & ((1 << 48) - 1)
    desc |= (af << 10) | (ro << 7) | (el0 << 6) | (pxn << 53) | (uxn << 54)
    desc |= 0b11 if level == 3 else 0b01
    return desc


def table_desc(addr):
    return (addr & ((1 << 48) - 1) & ~0xFFF) | 0b11


class TablePool:
    """Hands out 4 KB table pages at random, non-repeating addresses."""

    def __init__(self, mem, rng, base, pages=4096):
        self.mem, self.rng, self.base = mem, rng, base
        self.free = list(range(pages))
        rng.shuffle(self.free)

    def alloc(self, size=4096):
        addr = self.base + self.free.pop() * 4096
        self.mem.write(addr, bytes(size))
        return addr


class Context:
    """One stage 1 address space: a root table and its T0SZ."""

    def __init__(self, pool, t0sz=16):
        self.pool, self.mem, self.t0sz = pool, pool.mem, t0sz
        self.start = 0 if t0sz <= 24 else 1 if t0sz <= 33 else 2
        self.root = pool.alloc()

    def _slot(self, va, level):
        """Address of the descriptor for `va` at `level`, creating the
        tables above it as needed."""
        table = self.root
        for lvl in range(self.start, level):
            slot = table + ((va >> LEVEL_SHIFT[lvl]) & 0x1FF) * 8
            desc = self.mem.read_qword(slot)
            if desc & 0b11 != 0b11:
                desc = table_desc(self.pool.alloc())
                self.mem.write_qword(slot, desc)
            table = desc & ((1 << 48) - 1) & ~0xFFF
        return table + ((va >> LEVEL_SHIFT[level]) & 0x1FF) * 8

    def map(self, va, pa, level=PAGE, **attrs):
        self.mem.write_qword(self._slot(va, level), leaf_desc(pa, level, **attrs))

    def poke(self, va, level, value):
        """Write a raw descriptor, for planting faulty entries."""
        self.mem.write_qword(self._slot(va, level), value)


def write_ste(mem, strtab_base, sid, cfg, cd_addr=0, valid=1, s1fmt=0):
    mem.write_qword(strtab_base + sid * 64,
                    valid | (cfg << 1) | (s1fmt << 4) | (cd_addr & ((1 << 48) - 1) & ~0x3F))


def write_cd(mem, cd_addr, ttb, t0sz=16, valid=1, aa64=1, tg0=0, epd0=0,
             endi=0, affd=0):
    mem.write_qword(cd_addr, t0sz | (tg0 << 6) | (epd0 << 14) | (endi << 15)
                    | (valid << 31) | (affd << 35) | (aa64 << 41))
    mem.write_qword(cd_addr + 8, ttb)
