"""Checks the sizing study's cache model against the RTL.

Not part of `make test`. Run with `make validate`, which builds several
cache configurations and runs this module on each.

For every workload in study/workloads.py:

  1. the workload's page tables are built in memory and its accesses
     are replayed through the RTL, one at a time, with all caches on
  2. the same workload is replayed through study/cachemodel.py with the
     same configuration
  3. the hit and miss counters the hardware keeps (SG_TLB_HIT and the
     others) must equal the model's, and so must the number of table
     reads, both in total and access by access

Timing. For every access the module also measures

    translation cycles   from the device's address handshake to the
                         forwarded address handshake
    read latency         for each table read, from its address handshake
                         to its last data beat

and splits the translation time into the time spent waiting for table
reads and the rest (the overhead). The study's latency model assumes the
overhead depends only on the lookup path (configuration cached or not,
TLB hit or not, walk length) and not on how slow the memory is. This
module checks that the overhead is the same for every access on a path,
and that the model's predicted total equals the measured total. With
SG_READ_LATENCY=n every table read is delayed by about n extra cycles,
so running at two latencies tests the assumption.

Results go to build/validation/<parameters>.json.
"""

import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import cocotb
from cocotb.triggers import RisingEdge, with_timeout
from cocotbext.axi import AxiResp

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "study"))
import cachemodel as M                              # noqa: E402
import workloads as W                               # noqa: E402

from sg_bench import Bench                          # noqa: E402
from sg_env import design_params                    # noqa: E402

TIMEOUT_NS = 200_000


class ReadLatency:
    """Pause generator for the R channel of the table port: after each
    table read is accepted, hold its data back for `extra` cycles."""

    def __init__(self, env, extra):
        self.env, self.extra = env, extra
        self.seen, self.left = 0, 0

    def __iter__(self):
        return self

    def __next__(self):
        if self.env.table_reads != self.seen:
            self.seen = self.env.table_reads
            self.left = self.extra
        if self.left:
            self.left -= 1
            return True
        return False


class Timing:
    """Records handshake times on the device side, the memory side and
    the table port."""

    def __init__(self, dut):
        self.dut = dut
        self.cycle = 0
        self.up, self.down = [], []           # address handshakes
        self.rd_ar, self.rd_r = [], []        # table reads: address, last data beat

    async def run(self):
        d = self.dut
        while True:
            await RisingEdge(d.clk)
            self.cycle += 1
            if (int(d.s_axi_arvalid.value) and int(d.s_axi_arready.value)) or \
               (int(d.s_axi_awvalid.value) and int(d.s_axi_awready.value)):
                self.up.append(self.cycle)
            if (int(d.m_axi_arvalid.value) and int(d.m_axi_arready.value)) or \
               (int(d.m_axi_awvalid.value) and int(d.m_axi_awready.value)):
                self.down.append(self.cycle)
            if int(d.t_axi_arvalid.value) and int(d.t_axi_arready.value):
                self.rd_ar.append(self.cycle)
            if int(d.t_axi_rvalid.value) and int(d.t_axi_rready.value) and int(d.t_axi_rlast.value):
                self.rd_r.append(self.cycle)


async def replay(dut, name):
    params = design_params()
    config = M.Config(params["TLB_ENTRIES"], params["WC_ENTRIES"],
                      params["CFG_ENTRIES"], params["TLB_POLICY"])
    extra = int(os.environ.get("SG_READ_LATENCY", "0"))
    workload = {f.__name__: f for f in W.ALL}[name]()

    b = Bench(dut, caches=True)
    b.check_events = False
    await b.start()
    env = b.env
    if extra:
        env.tram.read_if.r_channel.set_pause_generator(ReadLatency(env, extra))
    pa_of = {}
    for sid, maps in workload.mappings.items():
        ctx = b.add_stream(sid, asid=workload.asid(sid))
        for va, pa, level in maps:
            ctx.map(va, pa, level)
            pa_of[(sid, va)] = (pa, level)
    await env.clear_counters()

    timing = Timing(dut)
    cocotb.start_soon(timing.run())
    reads_per_access = []
    for sid, va, write in workload.accesses:
        n_rd = len(timing.rd_ar)
        if write:
            resp = await with_timeout(env.axi.write(va, bytes(8), user=sid), TIMEOUT_NS, "ns")
        else:
            resp = await with_timeout(env.axi.read(va, 8, user=sid), TIMEOUT_NS, "ns")
        assert resp.resp == AxiResp.OKAY, f"{name}: sid {sid} va {va:#x} failed"
        reads_per_access.append(len(timing.rd_ar) - n_rd)

    # ---- counters: hardware against model ----
    rtl = await env.counters()
    rtl["reads"] = sum(reads_per_access)
    model, trace = M.replay(workload, config, record=True)
    expected = {k: getattr(model, k) for k in ("tlb_hit", "tlb_miss", "wc_hit",
                                                "cfg_hit", "cfg_miss", "reads")}
    first_diff = next((i for i, (n, t) in enumerate(zip(reads_per_access, trace))
                       if n != 2 * t[0] + t[2]), None)

    # ---- timing: overhead per lookup path ----
    n = len(workload.accesses)
    assert len(timing.up) == n and len(timing.down) == n
    lat = [r - a for a, r in zip(timing.rd_ar, timing.rd_r)]
    overhead = defaultdict(set)
    k = 0
    for i, (fetched, hit, walk) in enumerate(trace):
        reads = reads_per_access[i]
        waited = sum(lat[k:k + reads])
        k += reads
        path = f"{'cfg fetched' if fetched else 'cfg cached'}, " \
               f"{'TLB hit' if hit else f'walk {walk} read(s)'}"
        overhead[path].add(timing.down[i] - timing.up[i] - waited)

    # the study's latency model, at the read latency measured here
    read_latency = min(lat) if lat else 0
    predicted = model.cycles(read_latency)
    measured = sum(d - u for u, d in zip(timing.up, timing.down))

    result = {
        "workload": name, "config": str(config), "params": config.params,
        "read_latency_extra": extra, "accesses": n,
        "rtl": rtl, "model": expected, "match": rtl == expected,
        "first_access_that_differs": first_diff,
        "table_read_latency": sorted(set(lat)),
        "overhead_by_path": {p: sorted(v) for p, v in sorted(overhead.items())},
        "translation_cycles": measured, "predicted_cycles": predicted,
    }
    out = ROOT / "build" / "validation"
    out.mkdir(parents=True, exist_ok=True)
    tag = config.params.replace(" ", "_").replace("=", "-")
    with open(out / f"{tag}_lat{extra}.jsonl", "a") as f:
        f.write(json.dumps(result) + "\n")

    dut._log.info("%s, %s: hardware %s", name, config, rtl)
    dut._log.info("%s, %s: model    %s", name, config, expected)
    for p, v in result["overhead_by_path"].items():
        dut._log.info("    overhead %-30s %s cycles", p, v)
    assert rtl == expected, f"{name}: hardware and model disagree (first at access {first_diff})"
    assert all(len(v) == 1 for v in overhead.values()), \
        f"{name}: overhead is not constant per path: {result['overhead_by_path']}"
    dut._log.info("%s: translation cycles %d measured, %d predicted by the model "
                  "(table read latency %d)", name, measured, predicted, read_latency)
    assert measured == predicted, f"{name}: latency model is off by {measured - predicted} cycles"


def _make(name):
    async def test(dut):
        await replay(dut, name)
    test.__name__ = test.__qualname__ = f"model_matches_rtl_{name}"
    return cocotb.test()(test)


_names = os.environ.get("SG_WORKLOADS", ",".join(f.__name__ for f in W.ALL)).split(",")
for _name in _names:
    globals()[f"model_matches_rtl_{_name}"] = _make(_name)
