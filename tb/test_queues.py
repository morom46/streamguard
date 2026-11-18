"""Command queue and event queue tests.

Command queue: software writes commands into memory and moves
CMDQ_PROD; the hardware fetches, executes and moves CMDQ_CONS.

    * commands are consumed in order, across the wrap of the queue
    * each invalidation command removes what it names and nothing else
      (checked by counting table reads on the next access)
    * an illegal command stops the queue until software acknowledges
    * a command never runs in the middle of a lookup

Event queue: the hardware writes a record for a fault and moves
EVENTQ_PROD; software reads it and moves EVENTQ_CONS.

    * record contents for every kind of fault
    * which faults are not recorded
    * overflow, and the interrupt

The bench also checks the event queue after every access in every
other translation test; here the records are read explicitly.
"""

import cocotb
from cocotb.triggers import ClockCycles, Combine, RisingEdge, with_timeout
from cocotbext.axi import AxiProt, AxiResp

import sg_model as model
import sg_queues as q
from sg_bench import (DATA_HI, PRIV, TIMEOUT_NS, UNPRIV, Bench,
                      HoldTableReads)
from sg_env import (CMDQ_CONS, CR0_CMDQEN, CR0_EVENTQEN, CR0_SMMUEN,
                    EVENTQ_CONS, EVENTQ_PROD, GERROR, GERROR_CMDQ_ERR, GERRORN,
                    IRQ_CTRL)
from sg_pgtable import (BLOCK_1G, BLOCK_2M, PAGE, Context, leaf_desc,
                        table_desc, write_cd, write_ste)

INSTR = int(AxiProt.INSTRUCTION)


async def reads(b, sid, va):
    """Table reads needed by a successful access to va."""
    await b.expect(model.FC_NONE, sid=sid, va=va)
    return b.last_reads


async def raw_read(b, sid, va):
    """Read without the model check. Returns (resp, downstream address)."""
    n = len(b.env.downstream)
    resp = await with_timeout(b.env.axi.read(va, 8, user=sid), TIMEOUT_NS, "ns")
    down = b.env.downstream[-1][1] if len(b.env.downstream) > n else None
    return resp, down


async def cons(b):
    """(read index with wrap bit, error code) from CMDQ_CONS."""
    value = await b.env.apb.read(CMDQ_CONS)
    return value & 0x1F, (value >> 24) & 0x7F


# ---------------------------------------------------------------------
# Command queue mechanics
# ---------------------------------------------------------------------
@cocotb.test()
async def commands_consumed_across_wrap(dut):
    """40 commands through a 16-entry queue, in batches of every size
    up to a full queue."""
    b = Bench(dut, caches=True)
    await b.start()
    for n in (1, 3, 16, 5, 15):
        extra = [q.prefetch_config(1), (q.CMD_PREFETCH_ADDR, 0), q.sync()]
        await b.cmds((extra * 6)[:n - 1])          # plus the closing CMD_SYNC
    assert await cons(b) == (b.cmd_prod, q.CERROR_NONE)
    assert b.env.table_reads == 40, "one read request per command"
    assert await b.env.apb.read(GERROR) == 0


@cocotb.test()
async def small_queues(dut):
    """LOG2SIZE = 2: both queues have four entries and wrap often."""
    b = Bench(dut, caches=True, queue_log2=2)
    await b.start()
    b.add_stream(1)
    for i in range(9):
        await b.cmds([q.sync(), q.tlbi_nh_all()])
        await b.expect(model.FC_TRANSLATION, sid=1, va=i << 12)     # one record each
    assert await cons(b) == (b.cmd_prod, q.CERROR_NONE)
    assert b.env.event_writes == 9


@cocotb.test()
async def commands_wait_for_cmdqen(dut):
    b = Bench(dut, caches=True)
    await b.start(cmdq=False)
    await b.cmds([], wait=False)                   # one CMD_SYNC
    await ClockCycles(dut.clk, 50)
    assert await cons(b) == (0, q.CERROR_NONE)
    assert b.env.table_reads == 0, "command fetched while the queue is disabled"
    await b.env.set_cr0(CR0_SMMUEN | CR0_CMDQEN | CR0_EVENTQEN)
    await b.cmds_done()


# ---------------------------------------------------------------------
# TLB invalidation commands
# ---------------------------------------------------------------------
@cocotb.test()
async def tlbi_nh_va(dut):
    """One page of one address space. Leaf = 1 keeps the walk cache,
    Leaf = 0 drops it too. Other pages and other ASIDs are untouched."""
    b = Bench(dut, caches=True)
    await b.start()
    ctx = b.add_stream(1)
    A, B, C = 0x20_1000, 0x20_2000, 0x60_3000      # A and B share a 2 MB region
    for k, va in enumerate((A, B, C)):
        ctx.map(va, DATA_HI | (k << 12))
        await reads(b, 1, va)

    await b.cmds([q.tlbi_nh_va(ctx.asid, A + 0x10, leaf=1)])
    assert await reads(b, 1, B) == 0
    assert await reads(b, 1, C) == 0
    assert await reads(b, 1, A) == 1, "TLB entry gone, walk cache kept: level 3 only"

    await b.cmds([q.tlbi_nh_va(ctx.asid, A, leaf=0)])
    assert await reads(b, 1, B) == 0, "B's TLB entry must survive"
    assert await reads(b, 1, C) == 0
    assert await reads(b, 1, A) == 4, "TLB and walk cache entry gone: full walk"

    await b.cmds([q.tlbi_nh_va(ctx.asid + 1, A, leaf=0)])           # another ASID
    assert await reads(b, 1, A) == 0


@cocotb.test()
async def tlbi_nh_va_inside_a_block(dut):
    """Any address inside a block removes the block's entry."""
    b = Bench(dut, caches=True)
    await b.start()
    ctx = b.add_stream(1)
    M2, G1, P = 0x0000_4020_0000, 0x0080_4000_0000, 0x7000
    ctx.map(M2, DATA_HI | 0x0001_0060_0000, BLOCK_2M)
    ctx.map(G1, DATA_HI | 0x0033_C000_0000, BLOCK_1G)
    ctx.map(P, DATA_HI | 0x7000)
    for va in (M2, G1, P):
        await reads(b, 1, va)

    await b.cmds([q.tlbi_nh_va(ctx.asid, M2 + 0x10_5000)])
    assert await reads(b, 1, G1 + 0x1234_5000) == 0
    assert await reads(b, 1, P) == 0
    assert await reads(b, 1, M2 + 0x1000) > 0

    await b.cmds([q.tlbi_nh_va(ctx.asid, G1 + 0x3FFF_F000)])
    assert await reads(b, 1, M2) == 0
    assert await reads(b, 1, G1) > 0


@cocotb.test()
async def tlbi_by_asid_and_by_address(dut):
    b = Bench(dut, caches=True)
    await b.start()
    s1, s2 = b.add_stream(1), b.add_stream(2)
    X, Y = 0x7000, 0x40_9000
    for n, ctx in enumerate((s1, s2)):
        ctx.map(X, DATA_HI | ((2 * n + 1) << 16))
        ctx.map(Y, DATA_HI | ((2 * n + 2) << 16))
    pages = [(1, X), (1, Y), (2, X), (2, Y)]
    for sid, va in pages:
        await reads(b, sid, va)

    # one ASID: stream 1 loses everything, stream 2 nothing
    await b.cmds([q.tlbi_nh_asid(s1.asid)])
    assert [await reads(b, 2, va) for va in (X, Y)] == [0, 0]
    assert [await reads(b, 1, va) for va in (X, Y)] == [4, 4], "TLB and walk cache of the ASID"

    # one address, every ASID
    await b.cmds([q.tlbi_nh_vaa(X, leaf=1)])
    assert [await reads(b, sid, Y) for sid in (1, 2)] == [0, 0]
    assert [await reads(b, sid, X) for sid in (1, 2)] == [1, 1], "leaf only: walk cache kept"


@cocotb.test()
async def tlbi_all_keeps_the_configuration(dut):
    b = Bench(dut, caches=True)
    await b.start()
    ctx = b.add_stream(1)
    ctx.map(0x5000, DATA_HI | 0x9000)
    assert await reads(b, 1, 0x5000) == 6
    for cmd in (q.tlbi_nh_all(), q.tlbi_nsnh_all()):
        assert await reads(b, 1, 0x5000) == 0
        await b.cmds([cmd])
        assert await reads(b, 1, 0x5000) == 4, "four-level walk, STE and CD still cached"


@cocotb.test()
async def stale_until_tlbi_and_sync(dut):
    """The architected sequence: change the table, send the TLBI, send
    CMD_SYNC, wait for CMDQ_CONS. Before it the old mapping is still
    used; after it the new one."""
    b = Bench(dut, caches=True)
    await b.start()
    ctx = b.add_stream(1)
    ctx.map(0x9000, DATA_HI | 0x11000)
    await reads(b, 1, 0x9000)

    ctx.map(0x9000, DATA_HI | 0x22000)
    _, down = await raw_read(b, 1, 0x9000)
    assert down == DATA_HI | 0x11000, "expected the cached (stale) translation"

    await b.cmds([q.tlbi_nh_va(ctx.asid, 0x9000)])
    e = await b.expect(model.FC_NONE, sid=1, va=0x9000)
    assert e.addr == DATA_HI | 0x22000


# ---------------------------------------------------------------------
# Configuration invalidation commands
# ---------------------------------------------------------------------
@cocotb.test()
async def cfgi_ste(dut):
    b = Bench(dut, caches=True)
    await b.start()
    s1, s2 = b.add_stream(1), b.add_stream(2)
    s1.map(0x5000, DATA_HI | 0x9000)
    s2.map(0x5000, DATA_HI | 0xA000)
    await reads(b, 1, 0x5000)
    await reads(b, 2, 0x5000)

    # stream 1 set to abort; the hardware still has the old entry cached
    write_ste(b.mem, b.strtab, 1, model.STE_CFG_ABORT)
    resp, _ = await raw_read(b, 1, 0x5000)
    assert resp.resp == AxiResp.OKAY, "expected the cached (stale) configuration"

    await b.cmds([q.cfgi_ste(1)])
    exp = await b.access(sid=1, va=0x5000)
    assert exp.fault and b.last_reads == 1, "STE read again, and it says abort"
    assert await reads(b, 2, 0x5000) == 0, "stream 2 must be untouched"

    # back to stage 1: the TLB entry was never invalidated, so only the
    # configuration is read
    write_ste(b.mem, b.strtab, 1, model.STE_CFG_S1, s1.cd_addr)
    await b.cmds([q.cfgi_ste(1)])
    assert await reads(b, 1, 0x5000) == 2, "STE and CD, then a TLB hit"


@cocotb.test()
async def cfgi_range_and_all(dut):
    b = Bench(dut, caches=True)
    await b.start()
    sids = (4, 5, 6, 9)                            # fills the configuration cache
    for sid in sids:
        b.add_stream(sid).map(0x3000, DATA_HI | (sid << 12))
        await reads(b, sid, 0x3000)
    assert [await reads(b, sid, 0x3000) for sid in sids] == [0, 0, 0, 0]

    await b.cmds([q.cfgi_ste_range(5, 1)])         # Range 1: StreamIDs 4 to 7
    assert await reads(b, 9, 0x3000) == 0
    assert [await reads(b, sid, 0x3000) for sid in (4, 5, 6)] == [2, 2, 2]

    await b.cmds([q.cfgi_all()])
    assert [await reads(b, sid, 0x3000) for sid in sids] == [2, 2, 2, 2]


@cocotb.test()
async def cfgi_cd(dut):
    """Context descriptor pointed at another table, with a new ASID."""
    b = Bench(dut, caches=True)
    await b.start()
    ctx = b.add_stream(1)
    ctx.map(0x5000, DATA_HI | 0x9000)
    await reads(b, 1, 0x5000)

    other = Context(b.pool)
    other.map(0x5000, DATA_HI | 0xB000)
    write_cd(b.mem, ctx.cd_addr, other.root, asid=ctx.asid + 1)
    _, down = await raw_read(b, 1, 0x5000)
    assert down == DATA_HI | 0x9000, "expected the cached (stale) configuration"

    for cmd in (q.cfgi_cd(1), q.cfgi_cd_all(1)):
        await b.cmds([cmd])
        e = await b.expect(model.FC_NONE, sid=1, va=0x5000)
        assert e.addr == DATA_HI | 0xB000
    assert b.last_reads == 2, "configuration read again, new ASID's TLB entry hit"


# ---------------------------------------------------------------------
# Errors and ordering
# ---------------------------------------------------------------------
@cocotb.test()
async def illegal_command_stops_the_queue(dut):
    b = Bench(dut, caches=True)
    await b.start()
    ctx = b.add_stream(1)
    ctx.map(0x9000, DATA_HI | 0x11000)
    await reads(b, 1, 0x9000)
    ctx.map(0x9000, DATA_HI | 0x22000)             # needs the TLBI below

    bad_index = b.cmd_prod
    bad_slot = b.cmdq_base + bad_index * 16
    await b.cmds([(0x7F, 0), q.tlbi_nh_all()], wait=False)
    await ClockCycles(dut.clk, 60)

    assert await cons(b) == (bad_index, q.CERROR_ILL), "CONS must stay on the bad command"
    gerror = await b.env.apb.read(GERROR)
    assert gerror & GERROR_CMDQ_ERR and not (await b.env.apb.read(GERRORN)) & GERROR_CMDQ_ERR
    assert b.env.irq["gerror"] == 1
    _, down = await raw_read(b, 1, 0x9000)
    assert down == DATA_HI | 0x11000, "the TLBI behind the bad command must not have run"

    # software replaces the bad command with a CMD_SYNC and acknowledges
    b.mem.write_qword(bad_slot, q.CMD_SYNC)
    await b.env.apb.write(GERRORN, gerror)
    await b.cmds_done()
    e = await b.expect(model.FC_NONE, sid=1, va=0x9000)
    assert e.addr == DATA_HI | 0x22000

    # a second error toggles GERROR back; it is active again
    bad_slot = b.cmdq_base + (b.cmd_prod % 16) * 16
    await b.cmds([(0x44, 0)], sync=False, wait=False)      # CMD_RESUME: no stall support
    await ClockCycles(dut.clk, 40)
    assert (await cons(b))[1] == q.CERROR_ILL
    assert not (await b.env.apb.read(GERROR)) & GERROR_CMDQ_ERR
    assert b.env.irq["gerror"] == 2
    b.mem.write_qword(bad_slot, q.CMD_SYNC)
    await b.env.apb.write(GERRORN, 0)
    await b.cmds_done()


@cocotb.test()
async def command_waits_for_lookup(dut):
    """A walk is held after reading the level 2 entry. Software changes
    that entry and sends TLBI + SYNC. The command must not start until
    the lookup has finished; the access in flight completes with the
    old page, and after CMD_SYNC is consumed the next access sees the
    new one."""
    b = Bench(dut, caches=True)
    await b.start()
    env = b.env
    ctx = b.add_stream(1)
    va = 0x0040_0000_5000
    old_pa, new_pa = DATA_HI | 0x1000, DATA_HI | 0x2000
    ctx.map(va, old_pa)
    new_l3 = b.pool.alloc()
    b.mem.write_qword(new_l3 + ((va >> 12) & 0x1FF) * 8, leaf_desc(new_pa, PAGE))

    hold = HoldTableReads(env, after=5)            # STE, CD, L0, L1, L2
    env.tram.read_if.ar_channel.set_pause_generator(hold)
    task = cocotb.start_soon(env.axi.read(va, 8, user=1))
    while env.table_reads < 5:
        await RisingEdge(dut.clk)

    b.mem.write_qword(ctx._slot(va, 2), table_desc(new_l3))
    await b.cmds([q.tlbi_nh_va(ctx.asid, va, leaf=0)], wait=False)
    await ClockCycles(dut.clk, 40)
    assert await cons(b) == (0, q.CERROR_NONE), "command ran during the lookup"
    assert env.table_reads == 5

    hold.active = False
    n_down = len(env.downstream)
    resp = await with_timeout(task, TIMEOUT_NS, "ns")
    assert resp.resp == AxiResp.OKAY
    assert env.downstream[n_down][1] == old_pa, "in-flight access uses the old walk"

    await b.cmds_done()
    e = await b.expect(model.FC_NONE, sid=1, va=va)
    assert e.addr == new_pa


@cocotb.test()
async def commands_during_traffic(dut):
    """Four streams read and write while invalidation commands arrive
    non-stop. No mapping changes, so every access must succeed with the
    right data: commands and lookups have to take turns cleanly."""
    b = Bench(dut, caches=True)
    await b.start(0.2)
    env, rng = b.env, b.rng
    ctxs, pages = {}, {}
    for sid in range(1, 5):
        ctxs[sid] = b.add_stream(sid)
        pages[sid] = []
        for k in range(6):
            va, pa = (k << 21) | (sid << 12), DATA_HI | (sid << 24) | (k << 12)
            ctxs[sid].map(va, pa)
            pages[sid].append((va, pa))

    stop = False
    sent = 0

    async def commander():
        nonlocal sent
        while not stop:
            sid = rng.randint(1, 4)
            asid, va = ctxs[sid].asid, rng.choice(pages[sid])[0]
            await b.cmds([rng.choice([
                q.tlbi_nh_va(asid, va, leaf=rng.randint(0, 1)), q.tlbi_nh_asid(asid),
                q.tlbi_nh_all(), q.cfgi_ste(sid), q.cfgi_all()])])
            sent += 1

    async def worker(sid):
        for _ in range(60):
            va, pa = rng.choice(pages[sid])
            off, n = rng.randrange(0xF00), rng.randint(1, 64)
            if rng.random() < 0.5:
                data = bytes(rng.getrandbits(8) for _ in range(n))
                resp = await env.axi.write(va + off, data, user=sid)
                assert resp.resp == AxiResp.OKAY
                assert b.mem.read(pa + off, n) == data
            else:
                resp = await env.axi.read(va + off, n, user=sid)
                assert resp.resp == AxiResp.OKAY
                assert resp.data == b.mem.read(pa + off, n)

    cmd_task = cocotb.start_soon(commander())
    workers = [cocotb.start_soon(worker(sid)) for sid in ctxs]
    await with_timeout(Combine(*workers), 2_000_000, "ns")
    stop = True
    await with_timeout(cmd_task, 200_000, "ns")
    dut._log.info("%d command batches during 240 accesses", sent)
    assert sent > 20
    assert await b.pop_events() == []


# ---------------------------------------------------------------------
# Event queue
# ---------------------------------------------------------------------
async def one_event(b, **access):
    await b.access(**access)
    events = await b.pop_events()
    assert len(events) == 1, events
    return events[0]


@cocotb.test()
async def event_record_contents(dut):
    b = Bench(dut)
    await b.start()
    b.check_events = False                         # records are read here instead
    ctx = b.add_stream(3)
    RO, NOAF, UXN, WIDE = 0x1000, 0x2000, 0x3000, 0x4000
    ctx.map(RO, DATA_HI | 0x1000, ro=1)
    ctx.map(NOAF, DATA_HI | 0x2000, af=0)
    ctx.map(UXN, DATA_HI | 0x3000, uxn=1)
    ctx.poke(WIDE, 3, leaf_desc(DATA_HI | 0x4000, PAGE) | (1 << 49))

    # (id, StreamID, address, read, instruction, privileged)
    e = await one_event(b, sid=3, va=0x5008)
    assert e.fields() == (model.FC_TRANSLATION, 3, 0x5008, True, False, False)
    assert e.raw[0] == (3 << 32) | 0x10 and e.raw[3] == 0

    e = await one_event(b, sid=3, va=RO + 0x20, write=True, prot=PRIV)
    assert e.fields() == (model.FC_PERMISSION, 3, RO + 0x20, False, False, True)

    e = await one_event(b, sid=3, va=UXN, prot=UNPRIV | INSTR)
    assert e.fields() == (model.FC_PERMISSION, 3, UXN, True, True, False)

    e = await one_event(b, sid=3, va=NOAF + 8, write=True)
    assert e.fields() == (model.FC_ACCESS, 3, NOAF + 8, False, False, False)

    e = await one_event(b, sid=3, va=WIDE, prot=PRIV)
    assert e.fields() == (model.FC_ADDR_SIZE, 3, WIDE, True, False, True)

    # configuration errors: event number and StreamID only
    e = await one_event(b, sid=200, va=0x7000, write=True)
    assert e.fields() == (model.FC_BAD_STREAMID, 200, 0, False, False, False)
    assert e.raw[1:] == (0, 0, 0)

    write_ste(b.mem, b.strtab, 5, model.STE_CFG_S1, 0, valid=0)
    e = await one_event(b, sid=5, va=0x7000)
    assert e.fields() == (model.FC_BAD_STE, 5, 0, False, False, False)

    b.add_stream(6, valid=0)
    e = await one_event(b, sid=6, va=0x7000)
    assert e.fields() == (model.FC_BAD_CD, 6, 0, False, False, False)


@cocotb.test()
async def faults_that_are_not_recorded(dut):
    b = Bench(dut, caches=True)
    await b.start()
    b.check_events = False

    # a stream set to abort
    write_ste(b.mem, b.strtab, 8, model.STE_CFG_ABORT)
    assert (await b.access(sid=8, va=0x1000)).fault
    assert await b.pop_events() == []

    # CD.R = 0: stage 1 faults are terminated but not recorded, whether
    # they come from a walk or from a TLB hit
    quiet = b.add_stream(2, r=0)
    quiet.map(0x2000, DATA_HI | 0x2000, ro=1)
    await b.expect(model.FC_TRANSLATION, sid=2, va=0x3000)
    await b.expect(model.FC_NONE, sid=2, va=0x2000)
    await b.expect(model.FC_PERMISSION, sid=2, va=0x2000, write=True)
    assert b.last_reads == 0, "expected the permission fault to come from the TLB"
    assert await b.pop_events() == []
    assert b.env.event_writes == 0

    # the same sequence with CD.R = 1 records both
    loud = b.add_stream(1)
    loud.map(0x2000, DATA_HI | 0x4000, ro=1)
    await b.expect(model.FC_TRANSLATION, sid=1, va=0x3000)
    await b.expect(model.FC_NONE, sid=1, va=0x2000)
    await b.expect(model.FC_PERMISSION, sid=1, va=0x2000, write=True)
    assert b.last_reads == 0
    assert [e.id for e in await b.pop_events()] == [model.FC_TRANSLATION, model.FC_PERMISSION]

    # CD.R = 0 does not silence configuration errors
    b.add_stream(4, r=0, valid=0)
    await b.expect(model.FC_BAD_CD, sid=4, va=0x1000)
    assert [e.id for e in await b.pop_events()] == [model.FC_BAD_CD]


@cocotb.test()
async def event_queue_disabled(dut):
    """EVENTQEN = 0: faults are terminated as usual, nothing is written."""
    b = Bench(dut)
    await b.start(evtq=False)
    b.check_events = False
    b.add_stream(1)
    for va in (0x1000, 0x2000, 0x3000):
        await b.expect(model.FC_TRANSLATION, sid=1, va=va)
    await b.expect(model.FC_BAD_STREAMID, sid=99, va=0)
    assert b.env.event_writes == 0
    assert await b.env.apb.read(EVENTQ_PROD) == 0


@cocotb.test()
async def event_queue_overflow(dut):
    b = Bench(dut, queue_log2=2)                   # four entries
    await b.start()
    b.check_events = False
    b.add_stream(1)

    for i in range(4):
        await b.expect(model.FC_TRANSLATION, sid=1, va=i << 12)
    assert await b.env.apb.read(EVENTQ_PROD) == 0x4, "full: index 0, wrap bit set"

    for i in (4, 5):                               # lost
        await b.expect(model.FC_TRANSLATION, sid=1, va=i << 12)
    assert await b.env.apb.read(EVENTQ_PROD) == 0x80000004, "overflow flag, set once"
    assert b.env.event_writes == 4

    assert [e.addr >> 12 for e in await b.pop_events()] == [0, 1, 2, 3]

    # acknowledge the overflow; recording resumes
    b.evt_ovack = 1
    await b.env.apb.write(EVENTQ_CONS, b.evt_cons | (1 << 31))
    await b.expect(model.FC_TRANSLATION, sid=1, va=0x9000)
    assert [e.addr for e in await b.pop_events()] == [0x9000]

    # a second overflow toggles the flag back to 0
    for i in range(5):
        await b.expect(model.FC_TRANSLATION, sid=1, va=(0x10 + i) << 12)
    assert (await b.env.apb.read(EVENTQ_PROD)) >> 31 == 0
    assert [e.addr >> 12 for e in await b.pop_events()] == [0x10, 0x11, 0x12, 0x13]


@cocotb.test()
async def event_queue_interrupt(dut):
    """One pulse when the queue goes from empty to not empty."""
    b = Bench(dut)
    await b.start()
    b.check_events = False
    b.add_stream(1)

    await b.expect(model.FC_TRANSLATION, sid=1, va=0x1000)
    await ClockCycles(dut.clk, 3)
    assert b.env.irq["eventq"] == 1
    await b.expect(model.FC_TRANSLATION, sid=1, va=0x2000)     # queue was not empty
    await ClockCycles(dut.clk, 3)
    assert b.env.irq["eventq"] == 1

    assert len(await b.pop_events()) == 2
    await b.expect(model.FC_TRANSLATION, sid=1, va=0x3000)
    await ClockCycles(dut.clk, 3)
    assert b.env.irq["eventq"] == 2

    await b.pop_events()
    await b.env.apb.write(IRQ_CTRL, 0)
    await b.expect(model.FC_TRANSLATION, sid=1, va=0x4000)
    await ClockCycles(dut.clk, 3)
    assert b.env.irq["eventq"] == 2, "interrupt disabled"
    assert len(await b.pop_events()) == 1
