"""Register block tests: reset values, CR0/CR0ACK, GBPA update protocol."""

import cocotb

from sg_env import (CR0, CR0ACK, GBPA, GBPA_ABORT, GBPA_UPDATE, IDR0, IDR1,
                    IDR5, STRTAB_BASE, STRTAB_BASE_CFG, STRTAB_BASE_HI, Env)


@cocotb.test()
async def reset_values(dut):
    env = Env(dut)
    await env.reset()
    assert await env.apb.read(IDR0) == 0x0140000A
    assert await env.apb.read(IDR1) == 0x00840008
    assert await env.apb.read(IDR5) == 0x00000015
    assert await env.apb.read(CR0) == 0
    assert await env.apb.read(CR0ACK) == 0
    assert await env.apb.read(GBPA) == 0


@cocotb.test()
async def id_registers_are_read_only(dut):
    env = Env(dut)
    await env.reset()
    for reg in (IDR0, IDR1, IDR5, CR0ACK):
        before = await env.apb.read(reg)
        await env.apb.write(reg, 0xFFFFFFFF)
        assert await env.apb.read(reg) == before


@cocotb.test()
async def cr0_write_mask_and_ack(dut):
    """Only SMMUEN, EVENTQEN and CMDQEN are writable; CR0ACK follows CR0."""
    env = Env(dut)
    await env.reset()
    await env.apb.write(CR0, 0xFFFFFFFF)
    assert await env.apb.read(CR0) == 0xD
    assert await env.apb.read(CR0ACK) == 0xD
    await env.apb.write(CR0, 0)
    assert await env.apb.read(CR0) == 0
    assert await env.apb.read(CR0ACK) == 0


@cocotb.test()
async def cr0_byte_strobes(dut):
    env = Env(dut)
    await env.reset()
    # byte 0 not strobed: nothing changes
    await env.apb.write(CR0, 0xFFFFFFFF, strb=0b1110)
    assert await env.apb.read(CR0) == 0
    await env.apb.write(CR0, 0x00000001, strb=0b0001)
    assert await env.apb.read(CR0) == 0x1


@cocotb.test()
async def gbpa_needs_update_bit(dut):
    env = Env(dut)
    await env.reset()
    # Written without UPDATE: ignored
    await env.apb.write(GBPA, GBPA_ABORT)
    assert await env.apb.read(GBPA) == 0
    # Written with UPDATE: takes effect, UPDATE reads back clear when done
    await env.apb.write(GBPA, GBPA_UPDATE | GBPA_ABORT)
    await env.set_gbpa_abort(True)
    assert await env.apb.read(GBPA) == GBPA_ABORT
    await env.set_gbpa_abort(False)
    assert await env.apb.read(GBPA) == 0


@cocotb.test()
async def strtab_base_registers(dut):
    """Base address keeps bits [47:6] only; the config keeps LOG2SIZE."""
    env = Env(dut)
    await env.reset()
    await env.apb.write(STRTAB_BASE, 0xFFFFFFFF)
    await env.apb.write(STRTAB_BASE_HI, 0xFFFFFFFF)
    await env.apb.write(STRTAB_BASE_CFG, 0xFFFFFFFF)
    assert await env.apb.read(STRTAB_BASE) == 0xFFFFFFC0
    assert await env.apb.read(STRTAB_BASE_HI) == 0x0000FFFF
    assert await env.apb.read(STRTAB_BASE_CFG) == 0x3F


@cocotb.test()
async def unmapped_offsets_raz_wi(dut):
    env = Env(dut)
    await env.reset()
    for addr in (0x08, 0x8C, 0x90, 0xFFC, 0x100A8):
        await env.apb.write(addr, 0xDEADBEEF)
        assert await env.apb.read(addr) == 0
    # and the write did not land anywhere it should not
    assert await env.apb.read(CR0) == 0
    assert await env.apb.read(GBPA) == 0
