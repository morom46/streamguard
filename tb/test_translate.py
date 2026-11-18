"""Translation tests: stream table, context descriptor and table walk.

Every access is predicted by the reference model in sg_model.py, which
reads the same memory the hardware reads. The test then checks

    * the response the device gets (OKAY / SLVERR)
    * the fault code and level reported inside the unit
    * for a forwarded access: the address seen downstream and the data
    * for a terminated access: that nothing went downstream

These tests run with the caches off, so each lookup must make exactly
the table reads the model predicts. The random tests also run with the
caches on; test_cache.py covers the caches themselves.
"""

import cocotb
from cocotb.triggers import Combine, RisingEdge, with_timeout
from cocotbext.axi import AxiProt, AxiResp

import sg_model as model
import sg_queues as q
from sg_bench import DATA_HI, PRIV, TIMEOUT_NS, UNPRIV, Bench, pattern
from sg_pgtable import (BLOCK_1G, BLOCK_2M, LEVEL_SHIFT, PAGE, Context,
                        leaf_desc, table_desc, write_cd, write_ste)


# ---------------------------------------------------------------------
# Stream table entry
# ---------------------------------------------------------------------
@cocotb.test()
async def ste_configurations(dut):
    """abort, bypass, stage 1, invalid, unsupported, StreamID out of range."""
    b = Bench(dut)
    await b.start()
    mem = b.mem

    write_ste(mem, b.strtab, 1, model.STE_CFG_ABORT)
    write_ste(mem, b.strtab, 2, model.STE_CFG_BYPASS)
    ctx = b.add_stream(3)
    ctx.map(0x5000, DATA_HI | 0xABC000)
    write_ste(mem, b.strtab, 4, model.STE_CFG_S1, ctx.cd_addr, valid=0)
    write_ste(mem, b.strtab, 5, 0b110, ctx.cd_addr)            # stage 2 only
    write_ste(mem, b.strtab, 6, 0b111, ctx.cd_addr)            # nested
    write_ste(mem, b.strtab, 7, model.STE_CFG_S1, ctx.cd_addr, s1fmt=1)

    for write in (False, True):
        exp = await b.access(sid=1, va=0x5000, write=write)
        assert exp.fault and exp.fcode == model.FC_NONE        # abort, no event
        exp = await b.access(sid=2, va=DATA_HI | 0x5000, write=write)
        assert not exp.fault and exp.addr == DATA_HI | 0x5000  # bypass
        exp = await b.expect(model.FC_NONE, sid=3, va=0x5008, write=write)
        assert exp.addr == DATA_HI | 0xABC008
        for sid in (0, 4, 5, 6, 7):                            # 0 was never written
            await b.expect(model.FC_BAD_STE, sid=sid, va=0x5000, write=write)
        for sid in (16, 17, 100, 255):
            await b.expect(model.FC_BAD_STREAMID, sid=sid, va=0x5000, write=write)


@cocotb.test()
async def cd_checks(dut):
    b = Bench(dut)
    await b.start()
    bad = {
        1: dict(valid=0), 2: dict(aa64=0), 3: dict(tg0=1), 4: dict(tg0=2),
        5: dict(endi=1), 6: dict(t0sz=15), 7: dict(t0sz=40), 8: dict(t0sz=0),
    }
    for sid, fields in bad.items():
        t0sz = fields.pop("t0sz", 16)
        ctx = Context(b.pool, 16)
        ctx.map(0x1000, DATA_HI | 0x1000)
        cd = b.pool.alloc()
        write_cd(b.mem, cd, ctx.root, t0sz=t0sz, **fields)
        write_ste(b.mem, b.strtab, sid, model.STE_CFG_S1, cd)
        await b.expect(model.FC_BAD_CD, sid=sid, va=0x1000)

    # EPD0: walks through TTB0 disabled -> translation fault, no walk
    ctx = b.add_stream(9, epd0=1)
    ctx.map(0x1000, DATA_HI | 0x1000)
    await b.expect(model.FC_TRANSLATION, sid=9, va=0x1000)


# ---------------------------------------------------------------------
# Successful walks
# ---------------------------------------------------------------------
@cocotb.test()
async def page_and_block_sizes(dut):
    """4 KB page, 2 MB block and 1 GB block: the offset inside the
    mapping comes from the input address."""
    b = Bench(dut)
    await b.start()
    ctx = b.add_stream(1)
    ctx.map(0x0000_1234_5000, DATA_HI | 0x0000_0BEE_F000, PAGE)
    ctx.map(0x0000_4020_0000, DATA_HI | 0x0001_0060_0000, BLOCK_2M)
    ctx.map(0x0080_4000_0000, DATA_HI | 0x0033_C000_0000, BLOCK_1G)

    for write in (False, True):
        e = await b.expect(model.FC_NONE, 3, sid=1, va=0x0000_1234_5FF0, write=write)
        assert e.addr == DATA_HI | 0x0000_0BEE_FFF0 and e.reads == 6
        e = await b.expect(model.FC_NONE, 2, sid=1, va=0x0000_4031_2340, write=write)
        assert e.addr == DATA_HI | 0x0001_0071_2340 and e.reads == 5
        e = await b.expect(model.FC_NONE, 1, sid=1, va=0x0080_7ABC_DEF8, write=write)
        assert e.addr == DATA_HI | 0x0033_FABC_DEF8 and e.reads == 4

    # top and bottom of the 48-bit input space
    ctx.map(0x0000_0000_0000, DATA_HI | 0x7000)
    ctx.map(0xFFFF_FFFF_F000, DATA_HI | 0x9000)
    await b.expect(model.FC_NONE, sid=1, va=0x0)
    await b.expect(model.FC_NONE, sid=1, va=0xFFFF_FFFF_FFF8)


@cocotb.test()
async def t0sz_sets_start_level_and_range(dut):
    b = Bench(dut)
    await b.start()
    # (t0sz, start level, table reads for a page)
    for sid, (t0sz, start, reads) in enumerate(
            [(16, 0, 6), (24, 0, 6), (25, 1, 5), (33, 1, 5), (34, 2, 4), (39, 2, 4)], 1):
        ctx = b.add_stream(sid, t0sz)
        assert ctx.start == start
        top = (1 << (64 - t0sz)) - 0x1000                # last page in range
        ctx.map(top, DATA_HI | (sid << 20))
        e = await b.expect(model.FC_NONE, 3, sid=sid, va=top + 0x10)
        assert e.reads == reads
        if t0sz == 16:
            continue        # the range is the whole 48-bit bus, nothing is outside
        # first address past the range: fault before any walk read
        e = await b.expect(model.FC_TRANSLATION, start, sid=sid, va=top + 0x1000)
        assert e.reads == 2
        await b.expect(model.FC_TRANSLATION, start, sid=sid, va=0xFFFF_FFFF_F000)


# ---------------------------------------------------------------------
# Faults
# ---------------------------------------------------------------------
@cocotb.test()
async def translation_faults_at_every_level(dut):
    b = Bench(dut)
    await b.start()
    ctx = b.add_stream(1)
    base = 0x0040_0000_0000
    # Give each level its own region and leave the entry there invalid
    for level in range(4):
        va = base + (level << 39)
        ctx.poke(va, level, 0)                         # creates the tables above
        await b.expect(model.FC_TRANSLATION, level, sid=1, va=va)
        ctx.poke(va, level, 0xFFFF_FFFF_FFFF_FFFE)     # bit 0 clear is all that counts
        await b.expect(model.FC_TRANSLATION, level, sid=1, va=va)

    # Reserved encodings: 0b01 at level 0 (no 512 GB block) and at level 3
    va = base + (5 << 39)
    ctx.poke(va, 0, leaf_desc(DATA_HI, 1))             # block-style descriptor at L0
    await b.expect(model.FC_TRANSLATION, 0, sid=1, va=va)
    va = base + (6 << 39)
    ctx.poke(va, 3, (DATA_HI | 0x1000) | (1 << 10) | (1 << 6) | 0b01)
    await b.expect(model.FC_TRANSLATION, 3, sid=1, va=va)


@cocotb.test()
async def access_flag_and_affd(dut):
    b = Bench(dut)
    await b.start()
    ctx = b.add_stream(1)
    ctx.map(0x1000, DATA_HI | 0x1000, af=0)
    ctx.map(0x20_0000, DATA_HI | 0x20_0000, BLOCK_2M, af=0)
    await b.expect(model.FC_ACCESS, 3, sid=1, va=0x1000)
    await b.expect(model.FC_ACCESS, 2, sid=1, va=0x20_0040)

    # Same tables through a context with AFFD set: no access flag fault
    cd = b.pool.alloc()
    write_cd(b.mem, cd, ctx.root, affd=1)
    write_ste(b.mem, b.strtab, 2, model.STE_CFG_S1, cd)
    await b.expect(model.FC_NONE, sid=2, va=0x1000)
    await b.expect(model.FC_NONE, sid=2, va=0x20_0040)


@cocotb.test()
async def permissions(dut):
    b = Bench(dut)
    await b.start()
    ctx = b.add_stream(1)
    RO, PRIV_ONLY, NOEXEC_U, NOEXEC_P = 0x1000, 0x2000, 0x3000, 0x4000
    ctx.map(RO, DATA_HI | 0x1000, ro=1)
    ctx.map(PRIV_ONLY, DATA_HI | 0x2000, el0=0)
    ctx.map(NOEXEC_U, DATA_HI | 0x3000, uxn=1)
    ctx.map(NOEXEC_P, DATA_HI | 0x4000, pxn=1)
    INSTR = int(AxiProt.INSTRUCTION)

    # read-only
    await b.expect(model.FC_NONE, sid=1, va=RO, write=False)
    await b.expect(model.FC_PERMISSION, 3, sid=1, va=RO, write=True)
    await b.expect(model.FC_PERMISSION, 3, sid=1, va=RO, write=True, prot=PRIV)
    # privileged only
    await b.expect(model.FC_PERMISSION, 3, sid=1, va=PRIV_ONLY, write=False)
    await b.expect(model.FC_PERMISSION, 3, sid=1, va=PRIV_ONLY, write=True)
    await b.expect(model.FC_NONE, sid=1, va=PRIV_ONLY, write=False, prot=PRIV)
    await b.expect(model.FC_NONE, sid=1, va=PRIV_ONLY, write=True, prot=PRIV)
    # execute never applies to instruction reads only
    await b.expect(model.FC_NONE, sid=1, va=NOEXEC_U, prot=UNPRIV)
    await b.expect(model.FC_PERMISSION, 3, sid=1, va=NOEXEC_U, prot=UNPRIV | INSTR)
    await b.expect(model.FC_NONE, sid=1, va=NOEXEC_U, prot=PRIV | INSTR)
    await b.expect(model.FC_PERMISSION, 3, sid=1, va=NOEXEC_P, prot=PRIV | INSTR)
    await b.expect(model.FC_NONE, sid=1, va=NOEXEC_P, prot=UNPRIV | INSTR)
    await b.expect(model.FC_NONE, sid=1, va=NOEXEC_U, write=True, prot=UNPRIV | INSTR)


@cocotb.test()
async def address_size_faults(dut):
    """Descriptor address bits above the 48-bit output size."""
    b = Bench(dut)
    await b.start()
    ctx = b.add_stream(1)
    va = 0x0010_0000_0000
    ctx.map(va, DATA_HI | 0x5000)
    slot = ctx._slot(va, 3)
    b.mem.write_qword(slot, b.mem.read_qword(slot) | (1 << 48))      # leaf
    await b.expect(model.FC_ADDR_SIZE, 3, sid=1, va=va)

    va2 = 0x0020_0000_0000
    ctx.map(va2, DATA_HI | 0x6000)
    slot = ctx._slot(va2, 1)                                         # table at L1
    b.mem.write_qword(slot, b.mem.read_qword(slot) | (1 << 51))
    await b.expect(model.FC_ADDR_SIZE, 1, sid=1, va=va2)

    # priority: an invalid descriptor is a translation fault even with
    # the high bits set, and address size beats access flag
    ctx.poke(0x0030_0000_0000, 3, 0xF << 48)
    await b.expect(model.FC_TRANSLATION, 3, sid=1, va=0x0030_0000_0000)
    ctx.poke(0x0030_0000_1000, 3, leaf_desc(DATA_HI, 3, af=0) | (1 << 49))
    await b.expect(model.FC_ADDR_SIZE, 3, sid=1, va=0x0030_0000_1000)
    # and access flag beats permission
    ctx.map(0x0030_0000_2000, DATA_HI | 0x8000, af=0, ro=1)
    await b.expect(model.FC_ACCESS, 3, sid=1, va=0x0030_0000_2000, write=True)


# ---------------------------------------------------------------------
# Streams
# ---------------------------------------------------------------------
@cocotb.test()
async def streams_are_isolated(dut):
    """Two devices, two address spaces. The same input address lands in
    different memory, and neither can reach the other's pages."""
    b = Bench(dut)
    await b.start()
    a, c = b.add_stream(1), b.add_stream(2)
    SHARED_VA, A_ONLY, C_ONLY = 0x7000, 0x10_0000, 0x20_0000
    a.map(SHARED_VA, DATA_HI | 0xA000)
    c.map(SHARED_VA, DATA_HI | 0xC000)
    a.map(A_ONLY, DATA_HI | 0xA1000)
    c.map(C_ONLY, DATA_HI | 0xC1000)

    ea = await b.expect(model.FC_NONE, sid=1, va=SHARED_VA, write=True)
    ec = await b.expect(model.FC_NONE, sid=2, va=SHARED_VA, write=True)
    assert ea.addr == DATA_HI | 0xA000 and ec.addr == DATA_HI | 0xC000

    secret = b.mem.read(DATA_HI | 0xA1000, 8)
    await b.expect(model.FC_TRANSLATION, sid=2, va=A_ONLY, write=True)
    await b.expect(model.FC_TRANSLATION, sid=2, va=A_ONLY, write=False)
    await b.expect(model.FC_TRANSLATION, sid=1, va=C_ONLY, write=False)
    assert b.mem.read(DATA_HI | 0xA1000, 8) == secret


@cocotb.test()
async def table_updates_take_effect(dut):
    """There is no caching yet, so a changed mapping is used at once.
    This becomes the invalidation test when the TLB is added."""
    b = Bench(dut)
    await b.start()
    ctx = b.add_stream(1)
    ctx.map(0x9000, DATA_HI | 0x11000)
    e = await b.expect(model.FC_NONE, sid=1, va=0x9000, write=True)
    assert e.addr == DATA_HI | 0x11000
    ctx.map(0x9000, DATA_HI | 0x22000)
    e = await b.expect(model.FC_NONE, sid=1, va=0x9000, write=True)
    assert e.addr == DATA_HI | 0x22000
    ctx.poke(0x9000, 3, 0)
    await b.expect(model.FC_TRANSLATION, 3, sid=1, va=0x9000)


@cocotb.test()
async def burst_uses_one_translation(dut):
    """A burst cannot cross a 4 KB boundary, so translating its start
    address covers every beat."""
    b = Bench(dut)
    await b.start()
    ctx = b.add_stream(1)
    ctx.map(0x4000, DATA_HI | 0x77000)
    await b.access(sid=1, va=0x4000, length=2048, write=True)     # 256 beats
    await b.access(sid=1, va=0x4800, length=2048, write=False)


# ---------------------------------------------------------------------
# Random
# ---------------------------------------------------------------------
def build_random_stream(b, sid):
    rng = b.rng
    t0sz = rng.choice([16, 16, 25, 34, rng.randint(16, 39)])
    ctx = b.add_stream(sid, t0sz, affd=int(rng.random() < 0.15))
    span = 1 << (64 - t0sz)
    targets = []
    leaves = []                                         # (va, level) of good mappings

    for _ in range(16):
        level = rng.choice([3, 3, 3, 2, 2, 1])
        level = max(level, ctx.start, 1)
        size = 1 << LEVEL_SHIFT[level]
        if size > span:
            continue
        va = rng.randrange(span // size) * size
        pa = DATA_HI | (rng.randrange((1 << 47) // size) * size)
        ctx.map(va, pa, level,
                af=int(rng.random() < 0.85), el0=int(rng.random() < 0.8),
                ro=int(rng.random() < 0.3), pxn=int(rng.random() < 0.2),
                uxn=int(rng.random() < 0.2))
        targets.append((va, size))
        leaves.append((va, level))

    for _ in range(8):                                  # faulty entries
        level = rng.randint(ctx.start, 3)
        size = 1 << LEVEL_SHIFT[level]
        va = rng.randrange(span // size) * size
        kind = rng.choice(["invalid", "reserved", "addr_size_leaf", "addr_size_table"])
        if kind == "invalid":
            value = rng.getrandbits(64) & ~1
        elif kind == "reserved":
            level = rng.choice([lv for lv in (0, 3) if lv >= ctx.start])
            size = 1 << LEVEL_SHIFT[level]
            va = rng.randrange(span // size) * size
            value = (DATA_HI | (1 << 10) | (1 << 6) | 0b01)
        elif kind == "addr_size_leaf":
            level = max(level, 1)
            value = leaf_desc(DATA_HI, level) | (rng.randint(1, 15) << 48)
        else:
            level = min(level, 2)
            value = table_desc(b.pool.alloc()) | (rng.randint(1, 15) << 48)
        size = 1 << LEVEL_SHIFT[level]
        va = va // size * size
        ctx.poke(va, level, value)
        targets.append((va, size))
    ctx.leaves = leaves
    return span, targets, ctx


def remap_random_leaf(b, ctx):
    """Point one existing mapping somewhere else with new attributes."""
    rng = b.rng
    va, level = rng.choice(ctx.leaves)
    size = 1 << LEVEL_SHIFT[level]
    pa = DATA_HI | (rng.randrange((1 << 47) // size) * size)
    ctx.map(va, pa, level,
            af=int(rng.random() < 0.85), el0=int(rng.random() < 0.8),
            ro=int(rng.random() < 0.3), pxn=int(rng.random() < 0.2),
            uxn=int(rng.random() < 0.2))
    return va, level


async def random_change(b, streams, stats):
    """Change something the hardware may have cached, then invalidate in
    one of the ways that is sufficient for that change."""
    rng = b.rng
    sid = rng.randint(1, 6)
    ctx = streams[sid][2]
    done = sum(stats.values())
    kind = (0.0, 0.7, 0.9)[done] if done < 3 else rng.random()   # each kind at least once
    if kind < 0.6 and ctx.leaves:
        # one mapping moved: any command that covers its TLB entry will do
        va, level = remap_random_leaf(b, ctx)
        size = 1 << LEVEL_SHIFT[level]
        va = (va & ~(size - 1)) + (rng.randrange(size) & ~0xFFF)    # any page in it
        cmd = rng.choice([
            q.tlbi_nh_va(ctx.asid, va, leaf=1), q.tlbi_nh_va(ctx.asid, va, leaf=1),
            q.tlbi_nh_va(ctx.asid, va, leaf=0), q.tlbi_nh_vaa(va, leaf=rng.randint(0, 1)),
            q.tlbi_nh_asid(ctx.asid), q.tlbi_nh_all(), q.tlbi_nsnh_all(), None])
        if cmd is None:
            await b.env.invalidate_all()               # StreamGuard's own register
        else:
            await b.cmds([cmd])
        stats["remap"] += 1
    elif kind < 0.8:
        # stream switched between translating and abort
        ctx.aborted = not getattr(ctx, "aborted", False)
        write_ste(b.mem, b.strtab, sid,
                  model.STE_CFG_ABORT if ctx.aborted else model.STE_CFG_S1, ctx.cd_addr)
        await b.cmds([rng.choice([q.cfgi_ste(sid), q.cfgi_ste_range(sid, rng.randint(0, 31)),
                                  q.cfgi_all()])])
        stats["ste"] += 1
    else:
        # CD.R flipped: this stream's faults stop or start being recorded
        cd0 = b.mem.read_qword(ctx.cd_addr)
        b.mem.write_qword(ctx.cd_addr, cd0 ^ (1 << 45))
        await b.cmds([rng.choice([q.cfgi_cd(sid), q.cfgi_cd_all(sid), q.cfgi_ste(sid)])])
        stats["cd"] += 1


def random_access(rng, span, targets, ctx=None):
    r = rng.random()
    if r < 0.75:
        va, size = rng.choice(targets)
        va += rng.randrange(size)
    elif r < 0.9:
        va = rng.randrange(span)
    else:
        va = rng.randrange(1 << 48)                     # may be out of range
    length = rng.randint(1, 64)
    length = min(length, 0x1000 - (va & 0xFFF))         # stay inside the page
    prot = rng.choice([UNPRIV, UNPRIV, PRIV, UNPRIV | 4, PRIV | 4])
    return va, length, rng.random() < 0.5, prot


async def random_run(dut, backpressure, n_access, caches=False):
    """Random tables with faulty entries, random accesses from six
    streams plus bypass, abort, invalid and out-of-range StreamIDs.

    With caches on, mappings and stream configurations are also changed
    now and then. Each change is followed by a command that invalidates
    just enough, as software must do, so the result must still match
    the model for every access.

    Either way the event queue is checked after every access."""
    b = Bench(dut, caches=caches)
    await b.start(backpressure)
    streams = {sid: build_random_stream(b, sid) for sid in range(1, 7)}
    write_ste(b.mem, b.strtab, 7, model.STE_CFG_BYPASS)
    write_ste(b.mem, b.strtab, 8, model.STE_CFG_ABORT)

    seen = {}
    total_reads = predicted_reads = 0
    stats = {"remap": 0, "ste": 0, "cd": 0}
    recent = {}
    for _ in range(n_access):
        if caches and b.rng.random() < 0.08:
            await random_change(b, streams, stats)
        sid = b.rng.choice([1, 2, 3, 4, 5, 6, 1, 2, 3, 4, 5, 6, 7, 8, 9, 40])
        if sid in streams and caches and sid in recent and b.rng.random() < 0.4:
            # locality: the same page again, or a neighbour in the same 2 MB region
            va = recent[sid]
            if b.rng.random() < 0.5:
                va = (va & ~0x1F_FFFF) | (b.rng.randrange(512) << 12) | (va & 0xFFF)
            length = min(b.rng.randint(1, 64), 0x1000 - (va & 0xFFF))
            write, prot = b.rng.random() < 0.5, b.rng.choice([UNPRIV, PRIV])
        elif sid in streams:
            va, length, write, prot = random_access(b.rng, *streams[sid])
        else:
            va, length, write, prot = random_access(b.rng, 1 << 48, [(DATA_HI, 1 << 20)])
        exp = await b.access(sid=sid, va=va, length=length, write=write, prot=prot)
        recent[sid] = va
        total_reads += b.last_reads
        predicted_reads += exp.reads
        key = (exp.fault, exp.fcode)
        seen[key] = seen.get(key, 0) + 1

    dut._log.info("outcomes: %s", ", ".join(
        f"{'fault ' if f else 'ok '}{model.FC_NAMES.get(c, hex(c))}={n}"
        for (f, c), n in sorted(seen.items())))
    for code in (model.FC_TRANSLATION, model.FC_ADDR_SIZE, model.FC_ACCESS,
                 model.FC_PERMISSION, model.FC_BAD_STE, model.FC_BAD_STREAMID):
        assert (True, code) in seen, f"random test never hit {model.FC_NAMES[code]}"
    assert seen.get((False, model.FC_NONE), 0) > n_access // 10

    dut._log.info("table reads: %d made, %d without caches; changes: %s",
                  total_reads, predicted_reads, stats)
    if caches:
        c = await b.env.counters()
        dut._log.info("counters: %s", c)
        assert c["tlb_hit"] > 0 and c["cfg_hit"] > 0 and c["wc_hit"] > 0, c
        assert stats["remap"] > 0 and stats["ste"] > 0 and stats["cd"] > 0, stats
        assert total_reads < predicted_reads


@cocotb.test()
async def random_tables_against_model(dut):
    await random_run(dut, None, 500)


@cocotb.test()
async def random_tables_against_model_with_backpressure(dut):
    await random_run(dut, 0.3, 300)


@cocotb.test()
async def random_tables_against_model_cached(dut):
    await random_run(dut, None, 600, caches=True)


@cocotb.test()
async def random_tables_against_model_cached_with_backpressure(dut):
    await random_run(dut, 0.3, 300, caches=True)


@cocotb.test()
async def concurrent_streams(dut):
    await concurrent_run(dut, caches=False)


@cocotb.test()
async def concurrent_streams_cached(dut):
    await concurrent_run(dut, caches=True)


async def concurrent_run(dut, caches):
    """Reads and writes from four streams at once, with stalls. Checks
    outcome and data; the per-access fault code is covered above."""
    b = Bench(dut, caches=caches)
    await b.start(0.25)
    rng = b.rng
    pages = 24
    ctxs = {}
    for sid in range(1, 5):
        ctx = b.add_stream(sid)
        for p in range(pages):
            if p % 6 != 5:                              # every sixth page unmapped
                ctx.map(p << 12, DATA_HI | (sid << 28) | (p << 12),
                        ro=int(p % 6 == 4))
        ctxs[sid] = ctx
    stats = {"ok": 0, "fault": 0}

    async def worker(sid):
        for _ in range(40):
            p = rng.randrange(pages)
            off = rng.randrange(0, 0x1000 - 64)
            va, length, write = (p << 12) | off, rng.randint(1, 64), rng.random() < 0.5
            exp = b.predict(sid, va, write, UNPRIV)
            if write:
                data = pattern(rng, length)
                resp = await b.env.axi.write(va, data, user=sid, awid=sid)
                if not exp.fault:
                    assert b.mem.read(exp.addr, length) == data
            else:
                resp = await b.env.axi.read(va, length, user=sid, arid=sid)
                if not exp.fault:
                    assert resp.data == b.mem.read(exp.addr, length)
            assert (resp.resp == AxiResp.SLVERR) == exp.fault, \
                f"sid {sid} va {va:#x} write={write}: expected {exp}"
            stats["fault" if exp.fault else "ok"] += 1

    tasks = [cocotb.start_soon(worker(s)) for s in ctxs]
    await with_timeout(Combine(*tasks), 40 * TIMEOUT_NS, "ns")
    assert stats["ok"] > 20 and stats["fault"] > 20, stats


@cocotb.test()
async def lookup_latency_baseline(dut):
    """Cycles from the device's request to the request leaving downstream,
    with no stalls and no caching. The figure the TLB has to beat."""
    b = Bench(dut)
    await b.start()
    ctx = b.add_stream(1)
    ctx.map(0x1000, DATA_HI | 0x1000, PAGE)
    ctx.map(0x4000_0000, DATA_HI | 0x4000_0000, BLOCK_1G)
    write_ste(b.mem, b.strtab, 2, model.STE_CFG_BYPASS)

    async def latency(sid, va):
        task = cocotb.start_soon(b.env.axi.read(va, 8, user=sid))
        t0 = t1 = None
        cycles = 0
        while t1 is None:
            await RisingEdge(dut.clk)
            cycles += 1
            assert cycles < 2000, f"sid {sid} va {va:#x} never went downstream"
            if t0 is None and int(dut.s_axi_arvalid.value) and int(dut.s_axi_arready.value):
                t0 = cycles
            if int(dut.m_axi_arvalid.value) and int(dut.m_axi_arready.value):
                t1 = cycles
        await task
        return t1 - t0

    results = {
        "4 KB page, 4-level walk": await latency(1, 0x1000),
        "1 GB block, 2-level walk": await latency(1, 0x4000_0000),
        "STE bypass": await latency(2, DATA_HI),
    }
    for name, cyc in results.items():
        dut._log.info("lookup latency, %s: %d cycles", name, cyc)
    assert results["4 KB page, 4-level walk"] > results["1 GB block, 2-level walk"] \
        > results["STE bypass"]
