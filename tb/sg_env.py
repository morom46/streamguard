"""Shared testbench pieces for StreamGuard.

    Env      clock, reset, AXI requester on s_axi, AXI memory on m_axi,
             APB driver, and a monitor on the downstream address channels
    Apb      minimal APB4 requester
"""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge
from cocotbext.axi import AxiBus, AxiMaster, AxiRam

# Register offsets (must match rtl/sg_pkg.sv)
IDR0, IDR1, IDR5 = 0x00, 0x04, 0x14
CR0, CR0ACK, GBPA = 0x20, 0x24, 0x44

CR0_SMMUEN = 1 << 0
CR0_EVENTQEN = 1 << 2
CR0_CMDQEN = 1 << 3
GBPA_ABORT = 1 << 20
GBPA_UPDATE = 1 << 31

RAM_SIZE = 1 << 16


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
                          dut.rst_n, reset_active_level=False, size=RAM_SIZE)

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

    # ---- register helpers -------------------------------------------
    async def set_gbpa_abort(self, abort):
        await self.apb.write(GBPA, GBPA_UPDATE | (GBPA_ABORT if abort else 0))
        while (await self.apb.read(GBPA)) & GBPA_UPDATE:
            pass

    async def set_cr0(self, value):
        await self.apb.write(CR0, value)
        while (await self.apb.read(CR0ACK)) != (value & 0xD):
            pass
