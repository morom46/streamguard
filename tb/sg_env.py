"""Shared testbench pieces for StreamGuard.

    Env      clock, reset, AXI requester on s_axi, AXI memory on m_axi,
             APB driver, and a monitor on the downstream address channels
    Apb      minimal APB4 requester
"""

import os
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge
from cocotbext.axi import AxiBus, AxiMaster, AxiRam

# Register offsets (must match rtl/sg_pkg.sv)
IDR0, IDR1, IDR5 = 0x00, 0x04, 0x14
CR0, CR0ACK, GBPA = 0x20, 0x24, 0x44
IRQ_CTRL, IRQ_CTRLACK, GERROR, GERRORN = 0x50, 0x54, 0x60, 0x64
STRTAB_BASE, STRTAB_BASE_HI, STRTAB_BASE_CFG = 0x80, 0x84, 0x88
CMDQ_BASE, CMDQ_BASE_HI, CMDQ_PROD, CMDQ_CONS = 0x90, 0x94, 0x98, 0x9C
EVENTQ_BASE, EVENTQ_BASE_HI = 0xA0, 0xA4
EVENTQ_PROD, EVENTQ_CONS = 0x100A8, 0x100AC            # register page 1
GERROR_CMDQ_ERR = 1 << 0
GERROR_EVENTQ_ABT = 1 << 2
IRQ_GERROR_EN = 1 << 0
IRQ_EVENTQ_EN = 1 << 2
SG_CTRL, SG_INV = 0xE00, 0xE04
SG_COUNTERS = {"tlb_hit": 0xE10, "tlb_miss": 0xE14, "wc_hit": 0xE18,
               "cfg_hit": 0xE1C, "cfg_miss": 0xE20}
SG_CTRL_CACHE_DIS = 1 << 0
SG_INV_ALL = 1 << 0
SG_INV_COUNTERS = 1 << 1

CR0_SMMUEN = 1 << 0
CR0_EVENTQEN = 1 << 2
CR0_CMDQEN = 1 << 3
GBPA_ABORT = 1 << 20
GBPA_UPDATE = 1 << 31

# The memory model is sparse and covers the whole 48-bit space. The
# bypass tests only use (and compare) the first RAM_SIZE bytes.
RAM_SIZE = 1 << 16
MEM_SIZE = 1 << 48


# Design parameters, as built (tb/Makefile passes PARAMS in SG_PARAMS)
DEFAULT_PARAMS = {"TLB_ENTRIES": 8, "WC_ENTRIES": 4, "CFG_ENTRIES": 4, "TLB_POLICY": 0}


def design_params():
    params = dict(DEFAULT_PARAMS)
    for item in os.environ.get("SG_PARAMS", "").split():
        name, value = item.split("=")
        params[name] = int(value)
    return params


class Apb:
    """APB4 requester: one transfer at a time, setup then access phase."""

    def __init__(self, dut):
        self.dut = dut
        self.clk = dut.clk
        dut.s_apb_psel.value = 0
        dut.s_apb_penable.value = 0
        dut.s_apb_pwrite.value = 0
        dut.s_apb_paddr.value = 0
        dut.s_apb_pwdata.value = 0
        dut.s_apb_pstrb.value = 0

    async def _transfer(self, addr, write, data=0, strb=0xF):
        d = self.dut
        await RisingEdge(self.clk)
        d.s_apb_paddr.value = addr
        d.s_apb_pwrite.value = int(write)
        d.s_apb_pwdata.value = data
        d.s_apb_pstrb.value = strb if write else 0
        d.s_apb_psel.value = 1
        d.s_apb_penable.value = 0
        await RisingEdge(self.clk)
        d.s_apb_penable.value = 1
        while True:
            await RisingEdge(self.clk)
            if int(d.s_apb_pready.value):
                break
        rdata = int(d.s_apb_prdata.value)
        assert int(d.s_apb_pslverr.value) == 0, "unexpected PSLVERR"
        d.s_apb_psel.value = 0
        d.s_apb_penable.value = 0
        return rdata

    async def write(self, addr, data, strb=0xF):
        await self._transfer(addr, True, data, strb)

    async def read(self, addr):
        return await self._transfer(addr, False)


class Gate:
    """Pause generator that can be switched on and off from a test."""

    def __init__(self):
        self.paused = False

    def __iter__(self):
        return self

    def __next__(self):
        return self.paused


def random_pause(rng, p):
    """Pause generator: stall with probability p each cycle."""
    while True:
        yield rng.random() < p


class Env:
    def __init__(self, dut, seed=None):
        self.dut = dut
        self.rng = random.Random(seed if seed is not None else cocotb.RANDOM_SEED)

        cocotb.start_soon(Clock(dut.clk, 10, "ns").start())

        self.apb = Apb(dut)
        self.axi = AxiMaster(AxiBus.from_prefix(dut, "s_axi"), dut.clk,
                             dut.rst_n, reset_active_level=False)
        self.ram = AxiRam(AxiBus.from_prefix(dut, "m_axi"), dut.clk,
                          dut.rst_n, reset_active_level=False, size=MEM_SIZE)
        # The unit's own port (table and command reads, event writes)
        # reaches the same physical memory
        self.tram = AxiRam(AxiBus.from_prefix(dut, "t_axi"), dut.clk,
                           dut.rst_n, reset_active_level=False,
                           mem=self.ram.mem)
        self.table_reads = 0         # read requests on the unit's port
        self.event_writes = 0        # write requests on the unit's port
        self.irq = {"eventq": 0, "gerror": 0}

        # Every address request that reaches downstream: (kind, addr, id)
        self.downstream = []
        self.w_beats = 0
        cocotb.start_soon(self._monitor())

    async def _monitor(self):
        d = self.dut
        while True:
            await RisingEdge(d.clk)
            if int(d.rst_n.value) == 0:
                continue
            if int(d.m_axi_awvalid.value) and int(d.m_axi_awready.value):
                self.downstream.append(
                    ("W", int(d.m_axi_awaddr.value), int(d.m_axi_awid.value)))
            if int(d.m_axi_arvalid.value) and int(d.m_axi_arready.value):
                self.downstream.append(
                    ("R", int(d.m_axi_araddr.value), int(d.m_axi_arid.value)))
            if int(d.m_axi_wvalid.value) and int(d.m_axi_wready.value):
                self.w_beats += 1
            if int(d.t_axi_arvalid.value) and int(d.t_axi_arready.value):
                self.table_reads += 1
            if int(d.t_axi_awvalid.value) and int(d.t_axi_awready.value):
                self.event_writes += 1
            if int(d.irq_eventq.value):
                self.irq["eventq"] += 1
            if int(d.irq_gerror.value):
                self.irq["gerror"] += 1

    async def reset(self):
        self.dut.rst_n.value = 0
        await ClockCycles(self.dut.clk, 5)
        self.dut.rst_n.value = 1
        await ClockCycles(self.dut.clk, 3)

    def add_backpressure(self, p_up=0.3, p_down=0.3):
        """Random stalls on every channel, both sides."""
        r = self.rng
        self.axi.write_if.aw_channel.set_pause_generator(random_pause(r, p_up))
        self.axi.write_if.w_channel.set_pause_generator(random_pause(r, p_up))
        self.axi.write_if.b_channel.set_pause_generator(random_pause(r, p_up))
        self.axi.read_if.ar_channel.set_pause_generator(random_pause(r, p_up))
        self.axi.read_if.r_channel.set_pause_generator(random_pause(r, p_up))
        self.ram.write_if.aw_channel.set_pause_generator(random_pause(r, p_down))
        self.ram.write_if.w_channel.set_pause_generator(random_pause(r, p_down))
        self.ram.write_if.b_channel.set_pause_generator(random_pause(r, p_down))
        self.ram.read_if.ar_channel.set_pause_generator(random_pause(r, p_down))
        self.ram.read_if.r_channel.set_pause_generator(random_pause(r, p_down))
        self.tram.read_if.ar_channel.set_pause_generator(random_pause(r, p_down))
        self.tram.read_if.r_channel.set_pause_generator(random_pause(r, p_down))
        self.tram.write_if.aw_channel.set_pause_generator(random_pause(r, p_down))
        self.tram.write_if.w_channel.set_pause_generator(random_pause(r, p_down))
        self.tram.write_if.b_channel.set_pause_generator(random_pause(r, p_down))

    # ---- register helpers -------------------------------------------
    async def set_gbpa_abort(self, abort):
        await self.apb.write(GBPA, GBPA_UPDATE | (GBPA_ABORT if abort else 0))
        while (await self.apb.read(GBPA)) & GBPA_UPDATE:
            pass

    async def set_strtab(self, base, log2size):
        await self.apb.write(STRTAB_BASE, base & 0xFFFFFFFF)
        await self.apb.write(STRTAB_BASE_HI, base >> 32)
        await self.apb.write(STRTAB_BASE_CFG, log2size)

    async def set_queues(self, cmdq_base, evtq_base, log2size):
        """Queue base registers: LOG2SIZE in [4:0], address above it.
        Pointers start at zero."""
        for reg, base in ((CMDQ_BASE, cmdq_base), (EVENTQ_BASE, evtq_base)):
            await self.apb.write(reg, (base & 0xFFFFFFE0) | log2size)
            await self.apb.write(reg + 4, base >> 32)
        for reg in (CMDQ_PROD, CMDQ_CONS, EVENTQ_PROD, EVENTQ_CONS):
            await self.apb.write(reg, 0)

    async def set_caches(self, enabled):
        await self.apb.write(SG_CTRL, 0 if enabled else SG_CTRL_CACHE_DIS)

    async def invalidate_all(self):
        await self.apb.write(SG_INV, SG_INV_ALL)

    async def clear_counters(self):
        await self.apb.write(SG_INV, SG_INV_COUNTERS)

    async def counters(self):
        return {name: await self.apb.read(off) for name, off in SG_COUNTERS.items()}

    async def set_cr0(self, value):
        await self.apb.write(CR0, value)
        while (await self.apb.read(CR0ACK)) != (value & 0xD):
            pass
