"""Datapath tests for milestone 1: bypass and terminate.

Policy under test (rtl/sg_xlate.sv):

    SMMUEN  GBPA.ABORT   result
      0         0        bypass, address and payload unchanged
      0         1        terminated with SLVERR, nothing goes downstream
      1         x        terminated (translation not implemented yet)
"""

import cocotb
from cocotb.triggers import ClockCycles, Combine, RisingEdge, with_timeout
from cocotbext.axi import AxiBurstType, AxiResp

from sg_env import CR0_SMMUEN, RAM_SIZE, Env, Gate

TIMEOUT_NS = 2_000_000


def pattern(rng, n):
    return bytes(rng.getrandbits(8) for _ in range(n))


@cocotb.test()
async def bypass_write_then_read(dut):
    """Data written through the unit lands at the same address and reads back."""
    env = Env(dut)
    await env.reset()

    for addr, length in ((0x0000, 8), (0x0100, 64), (0x0FF8, 16), (0x2003, 5)):
        data = pattern(env.rng, length)
        wr = await with_timeout(env.axi.write(addr, data), TIMEOUT_NS, "ns")
        assert wr.resp == AxiResp.OKAY
        assert env.ram.read(addr, length) == data, "data did not reach memory"
        rd = await with_timeout(env.axi.read(addr, length), TIMEOUT_NS, "ns")
        assert rd.resp == AxiResp.OKAY
        assert rd.data == data

    # Addresses seen downstream are the ones the device issued. The
    # requester aligns the first beat to the 8-byte bus width, and it
    # splits the access at 0x0FF8 in two because an AXI burst must not
    # cross a 4 KB boundary.
    seen = {a & ~0x7 for _, a, _ in env.downstream}
    assert seen == {0x0000, 0x0100, 0x0FF8, 0x1000, 0x2000}


@cocotb.test()
async def bypass_long_bursts(dut):
    """256-beat INCR bursts, the longest AXI4 allows."""
    env = Env(dut)
    await env.reset()
    data = pattern(env.rng, 256 * 8)
    wr = await with_timeout(env.axi.write(0x4000, data), TIMEOUT_NS, "ns")
    assert wr.resp == AxiResp.OKAY
    rd = await with_timeout(env.axi.read(0x4000, len(data)), TIMEOUT_NS, "ns")
    assert rd.data == data
    writes = [d for d in env.downstream if d[0] == "W"]
    assert len(writes) == 1, "a 2048-byte write should be a single burst"


@cocotb.test()
async def bypass_random_traffic_with_backpressure(dut):
    """Concurrent random reads and writes with stalls on every channel."""
    env = Env(dut)
    await env.reset()
    env.add_backpressure(0.3, 0.3)

    rng = env.rng
    shadow = bytearray(RAM_SIZE)
    n_workers, n_ops = 4, 40
    region = RAM_SIZE // n_workers          # each worker owns a region

    async def worker(w):
        base = w * region
        for _ in range(n_ops):
            length = rng.randint(1, 96)
            addr = base + rng.randrange(0, region - length)
            if rng.random() < 0.6:
                data = pattern(rng, length)
                resp = await env.axi.write(addr, data, awid=w)
                assert resp.resp == AxiResp.OKAY
                shadow[addr:addr + length] = data
            else:
                resp = await env.axi.read(addr, length, arid=w)
                assert resp.resp == AxiResp.OKAY
                assert resp.data == bytes(shadow[addr:addr + length]), \
                    f"read mismatch at {addr:#x}"

    tasks = [cocotb.start_soon(worker(w)) for w in range(n_workers)]
    await with_timeout(Combine(*tasks), 20 * TIMEOUT_NS, "ns")

    assert env.ram.read(0, RAM_SIZE) == bytes(shadow), "memory differs from model"


@cocotb.test()
async def write_data_before_address(dut):
    """AXI4 lets W beats arrive before AW. They must wait for the decision."""
    env = Env(dut)
    await env.reset()

    hold_aw = Gate()
    hold_aw.paused = True
    env.axi.write_if.aw_channel.set_pause_generator(hold_aw)

    data = pattern(env.rng, 32)
    task = cocotb.start_soon(env.axi.write(0x0200, data))

    # W is being offered, AW is held back: nothing may move downstream
    for _ in range(20):
        await RisingEdge(dut.clk)
        assert int(dut.m_axi_wvalid.value) == 0, "W forwarded before its AW"
        assert int(dut.s_axi_wready.value) == 0, "W accepted before its AW"

    hold_aw.paused = False
    resp = await with_timeout(task, TIMEOUT_NS, "ns")
    assert resp.resp == AxiResp.OKAY
    assert env.ram.read(0x0200, 32) == data


async def expect_terminated(env):
    """Reads and writes of several lengths must all come back SLVERR."""
    before = env.ram.read(0, RAM_SIZE)
    n_down = len(env.downstream)
    w_beats = env.w_beats

    for addr, length in ((0x0000, 8), (0x0300, 1), (0x1000, 200), (0x2000, 2048)):
        wr = await with_timeout(
            env.axi.write(addr, pattern(env.rng, length)), TIMEOUT_NS, "ns")
        assert wr.resp == AxiResp.SLVERR
        rd = await with_timeout(env.axi.read(addr, length), TIMEOUT_NS, "ns")
        assert rd.resp == AxiResp.SLVERR
        assert len(rd.data) == length, "wrong number of error beats"
        assert rd.data == bytes(length), "terminated read must not return data"

    assert len(env.downstream) == n_down, "a terminated request went downstream"
    assert env.w_beats == w_beats, "write data of a terminated burst went downstream"
    assert env.ram.read(0, RAM_SIZE) == before, "memory changed"


@cocotb.test()
async def gbpa_abort_terminates(dut):
    env = Env(dut)
    await env.reset()
    env.ram.write(0, pattern(env.rng, 4096))
    await env.set_gbpa_abort(True)
    await expect_terminated(env)

    # and back to bypass
    await env.set_gbpa_abort(False)
    data = pattern(env.rng, 16)
    assert (await env.axi.write(0x40, data)).resp == AxiResp.OKAY
    assert (await env.axi.read(0x40, 16)).data == data


@cocotb.test()
async def gbpa_abort_terminates_with_backpressure(dut):
    env = Env(dut)
    await env.reset()
    env.add_backpressure(0.4, 0.4)
    await env.set_gbpa_abort(True)
    await expect_terminated(env)


@cocotb.test()
async def enabled_without_translation_fails_closed(dut):
    """SMMUEN=1 with no translation logic yet: everything is terminated."""
    env = Env(dut)
    await env.reset()
    await env.set_cr0(CR0_SMMUEN)
    await expect_terminated(env)

    await env.set_cr0(0)
    data = pattern(env.rng, 16)
    assert (await env.axi.write(0x80, data)).resp == AxiResp.OKAY
    assert (await env.axi.read(0x80, 16)).data == data


@cocotb.test()
async def error_response_does_not_overtake_write(dut):
    """Same ID: an earlier bypassed write is still waiting for its B when a
    later write is terminated. The error response must come second."""
    env = Env(dut)
    await env.reset()

    hold_b = Gate()
    hold_b.paused = True
    env.ram.write_if.b_channel.set_pause_generator(hold_b)

    order = []

    async def do_write(tag, addr):
        resp = await env.axi.write(addr, pattern(env.rng, 16), awid=3)
        order.append((tag, resp.resp))

    first = cocotb.start_soon(do_write("bypassed", 0x100))
    while not any(d[0] == "W" for d in env.downstream):
        await RisingEdge(dut.clk)

    await env.set_gbpa_abort(True)
    second = cocotb.start_soon(do_write("terminated", 0x200))

    # Give the terminated write plenty of time to (wrongly) respond
    await ClockCycles(dut.clk, 60)
    assert order == [], "a response was sent while the first B was held"

    hold_b.paused = False
    await with_timeout(Combine(first, second), TIMEOUT_NS, "ns")
    assert order == [("bypassed", AxiResp.OKAY), ("terminated", AxiResp.SLVERR)]


@cocotb.test()
async def error_response_does_not_overtake_read(dut):
    """Read version of the ordering test."""
    env = Env(dut)
    await env.reset()
    env.ram.write(0x100, bytes(range(16)))

    hold_r = Gate()
    hold_r.paused = True
    env.ram.read_if.r_channel.set_pause_generator(hold_r)

    order = []

    async def do_read(tag, addr):
        resp = await env.axi.read(addr, 16, arid=5)
        order.append((tag, resp.resp, resp.data))

    first = cocotb.start_soon(do_read("bypassed", 0x100))
    while not any(d[0] == "R" for d in env.downstream):
        await RisingEdge(dut.clk)

    await env.set_gbpa_abort(True)
    second = cocotb.start_soon(do_read("terminated", 0x200))

    await ClockCycles(dut.clk, 60)
    assert order == [], "a response was sent while the first R was held"

    hold_r.paused = False
    await with_timeout(Combine(first, second), TIMEOUT_NS, "ns")
    assert order == [("bypassed", AxiResp.OKAY, bytes(range(16))),
                     ("terminated", AxiResp.SLVERR, bytes(16))]


@cocotb.test()
async def policy_changes_under_traffic(dut):
    """GBPA is flipped while traffic is running. Every transaction must be
    either fully forwarded (OKAY, visible in memory) or fully terminated
    (SLVERR, no effect). Nothing may hang."""
    env = Env(dut)
    await env.reset()
    env.add_backpressure(0.25, 0.25)

    rng = env.rng
    n_workers, n_ops, slot = 4, 30, 128
    stats = {"okay": 0, "slverr": 0}
    stop = False

    async def toggler():
        abort = False
        while not stop:
            await ClockCycles(dut.clk, rng.randint(20, 120))
            abort = not abort
            await env.set_gbpa_abort(abort)
        await env.set_gbpa_abort(False)

    async def worker(w):
        for i in range(n_ops):
            # every operation gets its own untouched slot of memory
            addr = (w * n_ops + i) * slot
            length = rng.randint(1, slot)
            data = pattern(rng, length)
            resp = await env.axi.write(addr, data, awid=w)
            if resp.resp == AxiResp.OKAY:
                stats["okay"] += 1
                assert env.ram.read(addr, length) == data
            else:
                assert resp.resp == AxiResp.SLVERR
                stats["slverr"] += 1
                assert env.ram.read(addr, length) == bytes(length), \
                    "terminated write reached memory"

            rd = await env.axi.read(addr, length, arid=w)
            if rd.resp == AxiResp.OKAY:
                assert rd.data == env.ram.read(addr, length)
            else:
                assert rd.resp == AxiResp.SLVERR
                assert rd.data == bytes(length)

    tog = cocotb.start_soon(toggler())
    tasks = [cocotb.start_soon(worker(w)) for w in range(n_workers)]
    await with_timeout(Combine(*tasks), 40 * TIMEOUT_NS, "ns")
    stop = True
    await with_timeout(tog, TIMEOUT_NS, "ns")

    dut._log.info("forwarded writes: %d, terminated writes: %d",
                  stats["okay"], stats["slverr"])
    assert stats["okay"] > 0 and stats["slverr"] > 0, \
        "test did not exercise both outcomes"


@cocotb.test()
async def reset_while_idle_recovers(dut):
    env = Env(dut)
    await env.reset()
    await env.set_gbpa_abort(True)
    assert (await env.axi.read(0, 8)).resp == AxiResp.SLVERR
    await env.reset()
    # reset state is bypass
    data = pattern(env.rng, 8)
    assert (await env.axi.write(0x10, data)).resp == AxiResp.OKAY
    assert (await env.axi.read(0x10, 8)).data == data
