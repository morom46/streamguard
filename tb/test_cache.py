"""Cache tests: TLB, walk cache and configuration cache.

What the caches must do:

    * never change the outcome of an access (that is also checked by the
      cached random tests in test_translate.py)
    * remove table reads: a TLB hit needs none, a walk cache hit needs
      one, a configuration cache hit skips the STE and CD reads
    * keep address spaces apart by ASID
    * keep serving a stale translation until software invalidates, and
      stop as soon as it does
    * not keep anything learned by a lookup that an invalidation
      overtook

Counter names: tlb_hit, tlb_miss, wc_hit, cfg_hit, cfg_miss.
"""

import cocotb
from cocotb.triggers import ClockCycles, RisingEdge, with_timeout
from cocotbext.axi import AxiProt, AxiResp

import sg_model as model
from sg_bench import (DATA_HI, PRIV, TIMEOUT_NS, UNPRIV, Bench,
                      HoldTableReads)
from sg_pgtable import BLOCK_1G, BLOCK_2M, PAGE, leaf_desc, table_desc

TLB_ENTRIES = 8          # must match the streamguard defaults
WC_ENTRIES = 4


async def raw_read(b, sid, va):
    """Read without the model check. Returns (resp, downstream address)."""
    n = len(b.env.downstream)
    resp = await with_timeout(b.env.axi.read(va, 8, user=sid), TIMEOUT_NS, "ns")
    down = b.env.downstream[-1][1] if len(b.env.downstream) > n else None
    return resp, down


async def counters(b):
    return await b.env.counters()


# ---------------------------------------------------------------------
# Hits and misses
# ---------------------------------------------------------------------
@cocotb.test()
async def tlb_hit_skips_walk(dut):
    b = Bench(dut, caches=True)
    await b.start()
    ctx = b.add_stream(1)
    ctx.map(0x5000, DATA_HI | 0x9000)

    await b.expect(model.FC_NONE, sid=1, va=0x5010)
    assert b.last_reads == 6, "first access: STE, CD, four levels"
    e = await b.expect(model.FC_NONE, sid=1, va=0x5FF0, write=True)
    assert b.last_reads == 0, "second access to the page must not read tables"
    assert e.addr == DATA_HI | 0x9FF0

    c = await counters(b)
    assert c == dict(tlb_hit=1, tlb_miss=1, wc_hit=0, cfg_hit=1, cfg_miss=1), c


@cocotb.test()
async def walk_cache_and_config_cache(dut):
    """Another page in the same 2 MB region: the configuration and the
    level 3 table address are cached, so only the level 3 entry is read."""
    b = Bench(dut, caches=True)
    await b.start()
    ctx = b.add_stream(1)
    for k in range(4):
        ctx.map(0x20_0000 + (k << 12), DATA_HI | (0x30_0000 + (k << 12)))

    await b.expect(model.FC_NONE, sid=1, va=0x20_0000)
    assert b.last_reads == 6
    for k in range(1, 4):
        await b.expect(model.FC_NONE, sid=1, va=0x20_0000 + (k << 12) + 8)
        assert b.last_reads == 1, f"page {k}: expected one read (level 3 only)"

    # a page in a different 2 MB region: config cached, walk cache misses
    ctx.map(0x40_0000, DATA_HI | 0x50_0000)
    await b.expect(model.FC_NONE, sid=1, va=0x40_0000)
    assert b.last_reads == 4, "config cached, four-level walk"

    c = await counters(b)
    assert c == dict(tlb_hit=0, tlb_miss=5, wc_hit=3, cfg_hit=4, cfg_miss=1), c


@cocotb.test()
async def block_entries_cover_the_whole_block(dut):
    b = Bench(dut, caches=True)
    await b.start()
    ctx = b.add_stream(1)
    ctx.map(0x0000_4020_0000, DATA_HI | 0x0001_0060_0000, BLOCK_2M)
    ctx.map(0x0080_4000_0000, DATA_HI | 0x0033_C000_0000, BLOCK_1G)

    await b.expect(model.FC_NONE, 2, sid=1, va=0x0000_4020_0040)
    assert b.last_reads == 5
    e = await b.expect(model.FC_NONE, 2, sid=1, va=0x0000_403F_FFF8, write=True)
    assert b.last_reads == 0 and e.addr == DATA_HI | 0x0001_007F_FFF8

    await b.expect(model.FC_NONE, 1, sid=1, va=0x0080_4000_0000)
    assert b.last_reads == 2, "config cached, walk levels 0 and 1"
    e = await b.expect(model.FC_NONE, 1, sid=1, va=0x0080_7FFF_FFF0)
    assert b.last_reads == 0 and e.addr == DATA_HI | 0x0033_FFFF_FFF0

    # just outside each block: not a hit
    ctx.map(0x0000_4040_0000, DATA_HI | 0x7000)
    await b.expect(model.FC_NONE, 3, sid=1, va=0x0000_4040_0000)
    assert b.last_reads > 0


@cocotb.test()
async def permissions_checked_on_hit(dut):
    """The cached entry keeps the permission bits and every hit is
    checked against the access that hits."""
    b = Bench(dut, caches=True)
    await b.start()
    ctx = b.add_stream(1)
    RO, PRIV_ONLY, NOEXEC_U = 0x1000, 0x2000, 0x3000
    ctx.map(RO, DATA_HI | 0x1000, ro=1)
    ctx.map(PRIV_ONLY, DATA_HI | 0x2000, el0=0)
    ctx.map(NOEXEC_U, DATA_HI | 0x3000, uxn=1)
    INSTR = int(AxiProt.INSTRUCTION)

    await b.expect(model.FC_NONE, sid=1, va=RO)
    await b.expect(model.FC_PERMISSION, 3, sid=1, va=RO, write=True)
    assert b.last_reads == 0, "permission fault from the TLB, no walk"

    # a walk that ends in a permission fault still fills the TLB
    await b.expect(model.FC_PERMISSION, 3, sid=1, va=PRIV_ONLY)
    assert b.last_reads > 0
    await b.expect(model.FC_NONE, sid=1, va=PRIV_ONLY, prot=PRIV)
    assert b.last_reads == 0
    await b.expect(model.FC_PERMISSION, 3, sid=1, va=PRIV_ONLY, write=True)
    assert b.last_reads == 0

    await b.expect(model.FC_NONE, sid=1, va=NOEXEC_U)
    await b.expect(model.FC_PERMISSION, 3, sid=1, va=NOEXEC_U, prot=UNPRIV | INSTR)
    assert b.last_reads == 0
    await b.expect(model.FC_NONE, sid=1, va=NOEXEC_U, prot=PRIV | INSTR)
    assert b.last_reads == 0

    # the same on block entries (found missing by functional coverage)
    M2, G1 = 0x0000_4020_0000, 0x0080_4000_0000
    ctx.map(M2, DATA_HI | 0x0001_0060_0000, BLOCK_2M, ro=1)
    ctx.map(G1, DATA_HI | 0x0033_C000_0000, BLOCK_1G, ro=1)
    for va, level in ((M2 + 0x1_2340, 2), (G1 + 0x123_4560, 1)):
        await b.expect(model.FC_NONE, level, sid=1, va=va)
        await b.expect(model.FC_PERMISSION, level, sid=1, va=va + 0x1000, write=True)
        assert b.last_reads == 0, "permission fault must come from the block's TLB entry"


@cocotb.test()
async def faults_are_not_cached(dut):
    """A translation or access flag fault leaves nothing in the TLB, so
    fixing the entry needs no invalidation."""
    b = Bench(dut, caches=True)
    await b.start()
    ctx = b.add_stream(1)
    ctx.poke(0x7000, 3, 0)
    await b.expect(model.FC_TRANSLATION, 3, sid=1, va=0x7000)
    await b.expect(model.FC_TRANSLATION, 3, sid=1, va=0x7000)
    assert b.last_reads == 1, "walk cache helps, but the leaf is read again"
    ctx.map(0x7000, DATA_HI | 0x7000)
    await b.expect(model.FC_NONE, sid=1, va=0x7000)

    ctx.map(0x8000, DATA_HI | 0x8000, af=0)
    await b.expect(model.FC_ACCESS, 3, sid=1, va=0x8000)
    ctx.map(0x8000, DATA_HI | 0x8000, af=1)
    await b.expect(model.FC_NONE, sid=1, va=0x8000)

    # configuration faults are not cached either
    from sg_pgtable import write_ste
    write_ste(b.mem, b.strtab, 2, model.STE_CFG_S1, 0, valid=0)
    await b.expect(model.FC_BAD_STE, sid=2, va=0x1000)
    write_ste(b.mem, b.strtab, 2, model.STE_CFG_BYPASS)
    await b.expect(model.FC_NONE, sid=2, va=DATA_HI | 0x1000)


@cocotb.test()
async def abort_and_bypass_configs_are_cached(dut):
    from sg_pgtable import write_ste
    b = Bench(dut, caches=True)
    await b.start()
    write_ste(b.mem, b.strtab, 1, model.STE_CFG_ABORT)
    write_ste(b.mem, b.strtab, 2, model.STE_CFG_BYPASS)
    for _ in range(2):
        await b.access(sid=1, va=0x1000)
        await b.access(sid=2, va=DATA_HI | 0x1000)
    assert b.last_reads == 0
    c = await counters(b)
    assert c["cfg_hit"] == 2 and c["cfg_miss"] == 2, c


# ---------------------------------------------------------------------
# Address spaces
# ---------------------------------------------------------------------
@cocotb.test()
async def asid_keeps_streams_apart(dut):
    """Same input address in two streams with different ASIDs: both
    cached, each hit returns its own stream's memory."""
    b = Bench(dut, caches=True)
    await b.start()
    s1, s2 = b.add_stream(1), b.add_stream(2)
    s1.map(0x7000, DATA_HI | 0xA000)
    s2.map(0x7000, DATA_HI | 0xC000)
    for _ in range(3):
        e = await b.expect(model.FC_NONE, sid=1, va=0x7000, write=True)
        assert e.addr == DATA_HI | 0xA000
        e = await b.expect(model.FC_NONE, sid=2, va=0x7000, write=True)
        assert e.addr == DATA_HI | 0xC000
    assert b.last_reads == 0
    c = await counters(b)
    assert c["tlb_hit"] == 4, c


@cocotb.test()
async def same_asid_shares_translations(dut):
    """Two streams given the same ASID share TLB entries: the second
    stream gets the first one's translation. This is the architected
    behaviour, and why software must give separate address spaces
    separate ASIDs."""
    b = Bench(dut, caches=True)
    await b.start()
    s1 = b.add_stream(1, asid=7)
    s2 = b.add_stream(2, asid=7)
    s1.map(0x7000, DATA_HI | 0xA000)
    s2.map(0x7000, DATA_HI | 0xC000)

    await b.expect(model.FC_NONE, sid=1, va=0x7000)
    resp, down = await raw_read(b, 2, 0x7000)
    assert resp.resp == AxiResp.OKAY
    assert down == DATA_HI | 0xA000, f"expected stream 1's page, got {down:#x}"

    await b.env.invalidate_all()
    e = await b.expect(model.FC_NONE, sid=2, va=0x7000)
    assert e.addr == DATA_HI | 0xC000


# ---------------------------------------------------------------------
# Invalidation
# ---------------------------------------------------------------------
@cocotb.test()
async def stale_until_invalidated(dut):
    b = Bench(dut, caches=True)
    await b.start()
    ctx = b.add_stream(1)
    ctx.map(0x9000, DATA_HI | 0x11000)
    await b.expect(model.FC_NONE, sid=1, va=0x9000)

    # change the mapping without telling the hardware: still the old page
    ctx.map(0x9000, DATA_HI | 0x22000)
    _, down = await raw_read(b, 1, 0x9000)
    assert down == DATA_HI | 0x11000, "expected the cached (stale) translation"

    await b.env.invalidate_all()
    e = await b.expect(model.FC_NONE, sid=1, va=0x9000)
    assert e.addr == DATA_HI | 0x22000
    assert b.last_reads == 6, "invalidation must empty all three caches"

    # unmapping works the same way
    ctx.poke(0x9000, 3, 0)
    _, down = await raw_read(b, 1, 0x9000)
    assert down == DATA_HI | 0x22000
    await b.env.invalidate_all()
    await b.expect(model.FC_TRANSLATION, 3, sid=1, va=0x9000)


@cocotb.test()
async def invalidate_during_walk(dut):
    """The walk has read the old level 2 entry when software repoints it
    and invalidates. The walk finishes with the old translation (the
    access was already in flight), but must not leave it in the TLB or
    walk cache: the next access has to see the new mapping."""
    b = Bench(dut, caches=True)
    await b.start()
    env = b.env
    ctx = b.add_stream(1)
    va = 0x0040_0000_5000
    old_pa, new_pa = DATA_HI | 0x1000, DATA_HI | 0x2000
    ctx.map(va, old_pa)

    new_l3 = b.pool.alloc()
    b.mem.write_qword(new_l3 + ((va >> 12) & 0x1FF) * 8, leaf_desc(new_pa, PAGE))
    l2_slot = ctx._slot(va, 2)

    # STE, CD, L0, L1, L2 go through; the level 3 read is held
    hold = HoldTableReads(env, after=5)
    env.tram.read_if.ar_channel.set_pause_generator(hold)
    task = cocotb.start_soon(env.axi.read(va, 8, user=1))
    while env.table_reads < 5:
        await RisingEdge(dut.clk)
    await ClockCycles(dut.clk, 10)
    assert env.table_reads == 5

    b.mem.write_qword(l2_slot, table_desc(new_l3))
    await env.invalidate_all()
    hold.active = False

    n_down = len(env.downstream)
    resp = await with_timeout(task, TIMEOUT_NS, "ns")
    assert resp.resp == AxiResp.OKAY
    assert env.downstream[n_down - 1][1] == old_pa, "in-flight access uses the old walk"

    e = await b.expect(model.FC_NONE, sid=1, va=va)
    assert e.addr == new_pa


@cocotb.test()
async def caches_can_be_disabled(dut):
    b = Bench(dut, caches=False)
    await b.start()
    ctx = b.add_stream(1)
    ctx.map(0x5000, DATA_HI | 0x9000)
    for _ in range(3):
        await b.expect(model.FC_NONE, sid=1, va=0x5000)    # exact reads checked
    c = await counters(b)
    assert c["tlb_hit"] == 0 and c["cfg_hit"] == 0, c

    b.caches = True
    await b.env.set_caches(True)
    await b.expect(model.FC_NONE, sid=1, va=0x5000)
    assert b.last_reads == 6, "nothing was filled while disabled"
    await b.expect(model.FC_NONE, sid=1, va=0x5000)
    assert b.last_reads == 0

    # disabled again with entries present: they must not be used
    b.caches = False
    await b.env.set_caches(False)
    await b.expect(model.FC_NONE, sid=1, va=0x5000)        # exact reads checked
    ctx.map(0x5000, DATA_HI | 0xA000)
    e = await b.expect(model.FC_NONE, sid=1, va=0x5000)
    assert e.addr == DATA_HI | 0xA000


@cocotb.test()
async def counters_clear(dut):
    b = Bench(dut, caches=True)
    await b.start()
    ctx = b.add_stream(1)
    ctx.map(0x5000, DATA_HI | 0x9000)
    await b.access(sid=1, va=0x5000)
    await b.access(sid=1, va=0x5000)
    assert (await counters(b))["tlb_hit"] == 1
    await b.env.clear_counters()
    assert await counters(b) == dict(tlb_hit=0, tlb_miss=0, wc_hit=0, cfg_hit=0, cfg_miss=0)
    # clearing the counters does not empty the caches
    await b.access(sid=1, va=0x5000)
    assert b.last_reads == 0


# ---------------------------------------------------------------------
# Capacity
# ---------------------------------------------------------------------
@cocotb.test()
async def tlb_replacement(dut):
    """Fill every entry, then one more: the oldest fill is replaced."""
    b = Bench(dut, caches=True)
    await b.start()
    ctx = b.add_stream(1)
    pages = [(k << 21) | 0x3000 for k in range(TLB_ENTRIES + 1)]   # one per 2 MB region
    for k, va in enumerate(pages):
        ctx.map(va, DATA_HI | (k << 12))

    for va in pages[:TLB_ENTRIES]:
        await b.access(sid=1, va=va)
    for va in pages[:TLB_ENTRIES]:
        await b.access(sid=1, va=va)
        assert b.last_reads == 0, "all entries should still be cached"

    await b.access(sid=1, va=pages[TLB_ENTRIES])       # replaces entry 0
    await b.access(sid=1, va=pages[TLB_ENTRIES])
    assert b.last_reads == 0
    await b.access(sid=1, va=pages[0])
    assert b.last_reads > 0, "first page should have been replaced"
    await b.access(sid=1, va=pages[2])
    assert b.last_reads == 0


# ---------------------------------------------------------------------
# Latency
# ---------------------------------------------------------------------
@cocotb.test()
async def hit_latency(dut):
    """Cycles from the device's address handshake to the forwarded
    address handshake, memory without stalls."""
    b = Bench(dut, caches=True)
    await b.start()
    ctx = b.add_stream(1)
    ctx.map(0x1000, DATA_HI | 0x1000)
    ctx.map(0x2000, DATA_HI | 0x2000)

    async def latency(va):
        task = cocotb.start_soon(b.env.axi.read(va, 8, user=1))
        t0 = t1 = None
        cycles = 0
        while t1 is None:
            await RisingEdge(dut.clk)
            cycles += 1
            assert cycles < 2000
            if t0 is None and int(dut.s_axi_arvalid.value) and int(dut.s_axi_arready.value):
                t0 = cycles
            if int(dut.m_axi_arvalid.value) and int(dut.m_axi_arready.value):
                t1 = cycles
        await task
        return t1 - t0

    results = {
        "cold miss, 4 KB page": await latency(0x1000),
        "TLB hit": await latency(0x1008),
        "walk cache hit (same 2 MB region)": await latency(0x2000),
    }
    for name, cyc in results.items():
        dut._log.info("lookup latency, %s: %d cycles", name, cyc)
    assert results["TLB hit"] < results["walk cache hit (same 2 MB region)"] \
        < results["cold miss, 4 KB page"]
    assert results["TLB hit"] <= 8
