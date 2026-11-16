"""Test bench for translation tests: memory with a stream table,
helpers to add streams, and an access checked against the reference
model in sg_model.py."""

import cocotb
from cocotb.triggers import RisingEdge, with_timeout
from cocotbext.axi import AxiProt, AxiResp

import sg_model as model
from sg_env import CR0_SMMUEN, Env
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
    exactly, and the number of table reads may only be lower."""

    LOG2SIZE = 4             # 16 stream table entries

    def __init__(self, dut, caches=False):
        self.dut = dut
        self.caches = caches
        self.next_asid = 1
        self.last_reads = 0
        self.env = Env(dut)
        self.rng = self.env.rng
        self.mem = self.env.ram
        base = self.rng.randrange(1, 1 << 20) << 24          # below bit 47
        self.pool = TablePool(self.mem, self.rng, base)
        self.strtab = self.pool.alloc()
        self.rsp = []        # (fault, fcode, level) of every translation

    async def start(self, backpressure=None):
        await self.env.reset()
        if backpressure:
            self.env.add_backpressure(backpressure, backpressure)
        await self.env.set_strtab(self.strtab, self.LOG2SIZE)
        await self.env.set_caches(self.caches)
        await self.env.set_cr0(CR0_SMMUEN)
        cocotb.start_soon(self._probe())

    async def _probe(self):
        x = self.dut.u_xlate
        while True:
            await RisingEdge(self.dut.clk)
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
        return exp

    async def expect(self, fcode, level=None, **kw):
        exp = await self.access(**kw)
        assert exp.fcode == fcode and exp.fault == (fcode != model.FC_NONE), \
            f"test set-up error: model says {exp}"
        if level is not None:
            assert exp.level == level, f"test set-up error: model says {exp}"
        return exp
