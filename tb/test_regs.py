"""Register block tests: reset values, CR0/CR0ACK, GBPA update protocol."""

import cocotb

from sg_env import (CMDQ_BASE, CMDQ_BASE_HI, CMDQ_CONS, CMDQ_PROD, CR0, CR0ACK,
                    CR0_CMDQEN, CR0_EVENTQEN, EVENTQ_BASE, EVENTQ_BASE_HI,
                    EVENTQ_CONS, EVENTQ_PROD, GBPA, GBPA_ABORT, GBPA_UPDATE,
                    GERROR, GERRORN, IDR0, IDR1, IDR5, IRQ_CTRL, IRQ_CTRLACK,
                    SG_COUNTERS, SG_CTRL, SG_INV, STRTAB_BASE, STRTAB_BASE_CFG,
                    STRTAB_BASE_HI, Env)


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
async def cache_control_registers(dut):
    """SG_CTRL keeps CACHE_DIS only; SG_INV and the counters read as zero
    after reset and ignore writes other than SG_INV's action bits."""
    env = Env(dut)
    await env.reset()
    assert await env.apb.read(SG_CTRL) == 0
    await env.apb.write(SG_CTRL, 0xFFFFFFFF)
    assert await env.apb.read(SG_CTRL) == 1
    await env.apb.write(SG_CTRL, 0)
    assert await env.apb.read(SG_CTRL) == 0
    await env.apb.write(SG_INV, 0xFFFFFFFF)
    assert await env.apb.read(SG_INV) == 0
    for off in SG_COUNTERS.values():
        await env.apb.write(off, 0xFFFFFFFF)
        assert await env.apb.read(off) == 0


@cocotb.test()
async def unmapped_offsets_raz_wi(dut):
    env = Env(dut)
    await env.reset()
    for addr in (0x08, 0x8C, 0xB0, 0xFFC, 0x10000, 0x100A0, 0x100B0):
        await env.apb.write(addr, 0xDEADBEEF)
        assert await env.apb.read(addr) == 0
    # and the write did not land anywhere it should not
    assert await env.apb.read(CR0) == 0
    assert await env.apb.read(GBPA) == 0


@cocotb.test()
async def queue_registers(dut):
    """Queue base registers keep the implemented bits. The pointer the
    hardware owns can only be written while its queue is disabled."""
    env = Env(dut)
    await env.reset()
    for lo, hi in ((CMDQ_BASE, CMDQ_BASE_HI), (EVENTQ_BASE, EVENTQ_BASE_HI)):
        await env.apb.write(lo, 0xFFFFFFFF)
        await env.apb.write(hi, 0xFFFFFFFF)
        assert await env.apb.read(lo) == 0xFFFFFFFF      # ADDR[31:5], LOG2SIZE
        assert await env.apb.read(hi) == 0x0000FFFF      # ADDR[47:32]

    # queues disabled: every pointer is writable
    for reg in (CMDQ_PROD, CMDQ_CONS, EVENTQ_PROD, EVENTQ_CONS):
        await env.apb.write(reg, 0x15)
        assert await env.apb.read(reg) == 0x15
    await env.apb.write(EVENTQ_PROD, 0x80000003)         # overflow flag
    assert await env.apb.read(EVENTQ_PROD) == 0x80000003
    await env.apb.write(EVENTQ_CONS, 0x80000003)         # overflow acknowledge
    assert await env.apb.read(EVENTQ_CONS) == 0x80000003

    # queues enabled, empty (PROD == CONS): hardware-owned pointers are read-only
    await env.apb.write(CMDQ_PROD, 0x03)
    await env.apb.write(CMDQ_CONS, 0x03)
    await env.set_cr0(CR0_CMDQEN | CR0_EVENTQEN)
    await env.apb.write(CMDQ_CONS, 0x0A)
    await env.apb.write(EVENTQ_PROD, 0x0A)
    assert await env.apb.read(CMDQ_CONS) == 0x03
    assert await env.apb.read(EVENTQ_PROD) == 0x80000003
    await env.apb.write(EVENTQ_CONS, 0x04)               # software's pointer
    assert await env.apb.read(EVENTQ_CONS) == 0x04


@cocotb.test()
async def irq_and_error_registers(dut):
    env = Env(dut)
    await env.reset()
    await env.apb.write(IRQ_CTRL, 0xFFFFFFFF)
    assert await env.apb.read(IRQ_CTRL) == 0x5           # GERROR and EVENTQ enables
    assert await env.apb.read(IRQ_CTRLACK) == 0x5
    await env.apb.write(IRQ_CTRLACK, 0)
    assert await env.apb.read(IRQ_CTRLACK) == 0x5        # read-only
    await env.apb.write(GERROR, 0xFFFFFFFF)
    assert await env.apb.read(GERROR) == 0               # read-only
    await env.apb.write(GERRORN, 0xFFFFFFFF)
    assert await env.apb.read(GERRORN) == 0x5            # CMDQ_ERR, EVENTQ_ABT_ERR
