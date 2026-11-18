"""Test bench for translation tests: memory with a stream table, a
command queue and an event queue, helpers to add streams and send
commands, and an access checked against the reference model in
sg_model.py."""

import cocotb
from cocotb.triggers import RisingEdge, with_timeout
from cocotbext.axi import AxiProt, AxiResp

import sg_model as model
import sg_queues as q
from sg_coverage import Probe, cov
from sg_env import (CMDQ_CONS, CMDQ_PROD, CR0_CMDQEN, CR0_EVENTQEN, CR0_SMMUEN,
                    EVENTQ_CONS, EVENTQ_PROD, IRQ_CTRL, IRQ_EVENTQ_EN,
                    IRQ_GERROR_EN, Env)
from sg_pgtable import Context, TablePool, write_cd, write_ste

TIMEOUT_NS = 5_000_000
UNPRIV = int(AxiProt.NONSECURE)                      # unprivileged data access
PRIV = int(AxiProt.NONSECURE | AxiProt.PRIVILEGED)
DATA_HI = 1 << 47            # data lives in the upper half, tables in the lower


def pattern(rng, n):
    return bytes(rng.getrandbits(8) for _ in range(n))


class Bench:
    """Stream table in memory, unit enabled, every access checked
    against the reference model.

    caches=False turns the TLB, walk cache and configuration cache off,
    so every lookup reads exactly what the model says it reads.
    caches=True leaves them on: the outcome must still match the model
    exactly, and the number of table reads may only be lower.

    Both queues are enabled. After every access the event queue is
    read: a fault the model says is recorded must have produced exactly
    one record with the right contents, anything else none."""

    LOG2SIZE = 4             # 16 stream table entries

    def __init__(self, dut, caches=False, queue_log2=4):
        self.dut = dut
        self.caches = caches
        self.q_log2 = queue_log2
        self.cmd_prod = 0            # software's copy of CMDQ_PROD
        self.evt_cons = 0            # software's copy of EVENTQ_CONS
        self.evt_ovack = 0
        self.check_events = True
        self.next_asid = 1
        self.last_reads = 0
        self.env = Env(dut)
        self.rng = self.env.rng
        self.mem = self.env.ram
        base = self.rng.randrange(1, 1 << 20) << 24          # below bit 47
        self.pool = TablePool(self.mem, self.rng, base)
        self.strtab = self.pool.alloc()
        self.cmdq_base = self.pool.alloc()
        self.evtq_base = self.pool.alloc()
        self.rsp = []        # (fault, fcode, level) of every translation

    async def start(self, backpressure=None, cmdq=True, evtq=True):
        await self.env.reset()
        if backpressure:
            self.env.add_backpressure(backpressure, backpressure)
        await self.env.set_strtab(self.strtab, self.LOG2SIZE)
        await self.env.set_queues(self.cmdq_base, self.evtq_base, self.q_log2)
        await self.env.apb.write(IRQ_CTRL, IRQ_EVENTQ_EN | IRQ_GERROR_EN)
        await self.env.set_caches(self.caches)
        await self.env.set_cr0(CR0_SMMUEN | (CR0_CMDQEN if cmdq else 0)
                               | (CR0_EVENTQEN if evtq else 0))
        cocotb.start_soon(self._probe())

    # ---- queues ------------------------------------------------------
    @property
    def q_entries(self):
        return 1 << self.q_log2

    def _next(self, ptr):
        """Advance a queue pointer: index bits plus one wrap bit."""
        return (ptr + 1) % (2 * self.q_entries)

    def push_cmds(self, cmds):
        """Write commands into the queue memory. Returns the new PROD
        value; nothing happens until it is written to CMDQ_PROD."""
        for w0, w1 in cmds:
            slot = self.cmdq_base + (self.cmd_prod % self.q_entries) * 16
            self.mem.write_qword(slot, w0)
            self.mem.write_qword(slot + 8, w1)
            self.cmd_prod = self._next(self.cmd_prod)
        return self.cmd_prod

    async def cmds(self, cmds, sync=True, wait=True):
        """Send commands, by default followed by CMD_SYNC, and wait until
        the hardware has consumed them all."""
        cmds = list(cmds) + ([q.sync()] if sync else [])
        assert len(cmds) <= self.q_entries
        await self.env.apb.write(CMDQ_PROD, self.push_cmds(cmds))
        if wait:
            await self.cmds_done()

    async def cmds_done(self, polls=400):
        for _ in range(polls):
            cons = await self.env.apb.read(CMDQ_CONS)
            if cons & (2 * self.q_entries - 1) == self.cmd_prod:
                return
        raise AssertionError(f"commands not consumed, CMDQ_CONS = {cons:#x}")

    async def pop_events(self):
        """Read and consume every record in the event queue."""
        prod = (await self.env.apb.read(EVENTQ_PROD)) & (2 * self.q_entries - 1)
        events = []
        while self.evt_cons != prod:
            slot = self.evtq_base + (self.evt_cons % self.q_entries) * 32
            events.append(q.Event.parse(
                [self.mem.read_qword(slot + 8 * i) for i in range(4)]))
            self.evt_cons = self._next(self.evt_cons)
        if events:
            await self.env.apb.write(EVENTQ_CONS, self.evt_cons | (self.evt_ovack << 31))
        return events

    def expected_event(self, exp, sid, va, write, prot):
        """Fields of the record the model says this access leaves, or None."""
        if not exp.event:
            return None
        if exp.fcode in model.XLAT_FAULTS:
            return (exp.fcode, sid, va, not write,
                    bool(prot & model.PROT_INSTRUCTION), bool(prot & model.PROT_PRIVILEGED))
        return (exp.fcode, sid, 0, False, False, False)

    async def _probe(self):
        x = self.dut.u_xlate
        coverage = Probe(self.dut)
        while True:
            await RisingEdge(self.dut.clk)
            coverage.sample()
            if int(x.rsp_valid.value):
                self.rsp.append((bool(int(x.rsp_fault.value)),
                                 int(x.rsp_fcode.value), int(x.rsp_flevel.value)))

    def add_stream(self, sid, t0sz=16, asid=None, **cd_fields):
        """New address space for `sid`. Each one gets its own ASID unless
        one is given."""
        ctx = Context(self.pool, t0sz)
        if asid is None:
            asid = self.next_asid
            self.next_asid = self.next_asid % 255 + 1
        ctx.asid = asid
        cd = self.pool.alloc()
        write_cd(self.mem, cd, ctx.root, t0sz=t0sz, asid=asid, **cd_fields)
        write_ste(self.mem, self.strtab, sid, model.STE_CFG_S1, cd)
        ctx.cd_addr = cd
        return ctx

    def predict(self, sid, va, write, prot):
        return model.translate(self.mem.read_qword, self.strtab, self.LOG2SIZE,
                               sid, va, write, prot)

    async def access(self, sid, va, length=8, write=False, prot=UNPRIV):
        """One access, checked against the model. Returns the prediction."""
        env = self.env
        exp = self.predict(sid, va, write, prot)
        n_rsp, n_down, n_tab = len(self.rsp), len(env.downstream), env.table_reads
        data = pattern(self.rng, length)

        if write:
            resp = await with_timeout(
                env.axi.write(va, data, user=sid, prot=AxiProt(prot)), TIMEOUT_NS, "ns")
        else:
            if not exp.fault:
                self.mem.write(exp.addr, data)
            resp = await with_timeout(
                env.axi.read(va, length, user=sid, prot=AxiProt(prot)), TIMEOUT_NS, "ns")

        what = f"sid {sid} va {va:#x} {'W' if write else 'R'} prot {prot:#b}: expected {exp}"
        assert len(self.rsp) == n_rsp + 1, what
        assert self.rsp[-1] == (exp.fault, exp.fcode, exp.level), \
            f"{what}, hardware reported {self.rsp[-1]}"
        self.last_reads = env.table_reads - n_tab
        if self.caches:
            assert self.last_reads <= exp.reads, \
                f"{what}: {self.last_reads} table reads, uncached lookup needs {exp.reads}"
        else:
            assert self.last_reads == exp.reads, \
                f"{what}: {self.last_reads} table reads, expected {exp.reads}"

        if exp.fault:
            assert resp.resp == AxiResp.SLVERR, what
            assert len(env.downstream) == n_down, f"{what}: went downstream"
            if not write:
                assert resp.data == bytes(length)
        else:
            assert resp.resp == AxiResp.OKAY, what
            assert len(env.downstream) == n_down + 1, what
            assert env.downstream[-1][1] == exp.addr, \
                f"{what}: downstream address {env.downstream[-1][1]:#x}"
            if write:
                assert self.mem.read(exp.addr, length) == data, what
            else:
                assert resp.data == data, what

        cov.sample_access(exp, write, prot, va, length)

        if self.check_events:
            want = self.expected_event(exp, sid, va, write, prot)
            got = [e.fields() for e in await self.pop_events()]
            assert got == ([want] if want else []), \
                f"{what}: event queue has {got}, expected {want}"
        return exp

    async def expect(self, fcode, level=None, **kw):
        exp = await self.access(**kw)
        assert exp.fcode == fcode and exp.fault == (fcode != model.FC_NONE), \
            f"test set-up error: model says {exp}"
        if level is not None:
            assert exp.level == level, f"test set-up error: model says {exp}"
        return exp


class HoldTableReads:
    """Pause generator for the unit's read channel: once `after` reads
    have been accepted, stop accepting more while active."""

    def __init__(self, env, after):
        self.env, self.after, self.active = env, after, True

    def __iter__(self):
        return self

    def __next__(self):
        return self.active and self.env.table_reads >= self.after
