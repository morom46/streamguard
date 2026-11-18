"""Area of each cache configuration, from synthesis.

Each cache is synthesised on its own with Yosys, mapped to two-input
NAND and NOR gates, inverters and flip-flops, and the result converted
to NAND2 gate-equivalents (GE), the usual library-independent unit of
logic area:

    NAND2, NOR2                          1.0 GE
    inverter                             0.67 GE
    flip-flop with asynchronous reset    6.7 GE
    flip-flop without reset              5.7 GE
    clock enable on a flip-flop          +2.3 GE (a 2:1 multiplexer)

These ratios are typical of standard-cell libraries but belong to none
in particular, so the totals are estimates. They are good for comparing
configurations with each other, not for predicting silicon area. Real
TLBs of 32 entries and more are usually built from latch arrays or
small memories rather than flip-flops, which this estimate overstates.

The rest of the unit does not change with the cache sizes, so the area
of a configuration is

    rest of the unit + TLB(entries, policy) + walk cache(entries)
                     + configuration cache(entries)

The "rest" is taken from one synthesis of the whole unit. run_study.py
checks this sum against whole-unit syntheses of other configurations.

Results are cached in build/area.json.
"""

import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "build" / "area.json"

GE_NAND, GE_NOR, GE_NOT = 1.0, 1.0, 0.67
GE_FF_RESET, GE_FF, GE_ENABLE = 6.7, 5.7, 2.3

SID_W, ASID_W = 8, 8
WC_TAG_W, WC_DATA_W = ASID_W + 27, 36          # {ASID, VA[47:21]} -> table address
CFG_TAG_W, CFG_DATA_W = SID_W, 2 + 8 + 1 + 1 + 6 + 36

TLB_SIZES = [2, 4, 8, 16, 32, 64]
WC_SIZES = [1, 2, 4, 8, 16]
CFG_SIZES = [1, 2, 4, 8]

ALL_RTL = ["sg_pkg.sv", "sg_perm.sv", "sg_tagcache.sv", "sg_tlb.sv", "sg_apb_regs.sv",
           "sg_memrd.sv", "sg_cmdq.sv", "sg_evtq.sv", "sg_ptw.sv", "sg_xlate.sv",
           "sg_addr_chan.sv", "sg_tbu.sv", "streamguard.sv"]


def ge_of(cells):
    """Gate-equivalents of a {cell type: count} dictionary."""
    ge = 0.0
    for kind, n in cells.items():
        if kind == "$_NAND_":
            ge += n * GE_NAND
        elif kind == "$_NOR_":
            ge += n * GE_NOR
        elif kind == "$_NOT_":
            ge += n * GE_NOT
        elif kind == "$scopeinfo":
            pass                     # records the hierarchy after flattening; not logic
        elif kind.startswith("$_DFF"):
            # $_DFF_PN0_: clock edge, reset; $_DFFE_PN0P_: ... and enable
            m = re.fullmatch(r"\$_DFF(E?)_([PN])([PN][01])?([PN])?_", kind)
            if not m:
                raise ValueError(f"unexpected flip-flop type {kind}")
            ge += n * ((GE_FF_RESET if m.group(3) else GE_FF) + (GE_ENABLE if m.group(1) else 0))
        else:
            raise ValueError(f"unexpected cell type {kind}")
    return ge


def synth(top, files, params, key):
    """Synthesise one module. Returns {"ge", "cells", "ffs"}.

    Yosys runs from the repository root with relative paths: the PyPI
    build runs sandboxed and only sees files below its working directory.
    """
    chparam = " ".join(f"-set {k} {v}" for k, v in params.items())
    out = Path("build") / "area_tmp" / (key.replace("/", "_") + ".json")
    (ROOT / out).parent.mkdir(parents=True, exist_ok=True)
    script = (f"read_verilog -sv {' '.join('rtl/' + f for f in files)}; "
              + (f"chparam {chparam} {top}; " if chparam else "")
              + f"synth -flatten -top {top}; abc -g cmos2; opt_clean; "
              + f"tee -q -o {out} stat -json")
    run = subprocess.run(["yowasp-yosys", "-q", "-p", script], cwd=ROOT,
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if run.returncode:
        raise RuntimeError(f"synthesis of {key} failed:\n{run.stderr[-2000:]}")
    stat = json.loads((ROOT / out).read_text())
    cells = stat["design"]["num_cells_by_type"]
    return {"ge": round(ge_of(cells), 1),
            "cells": stat["design"]["num_cells"],
            "ffs": sum(n for k, n in cells.items() if k.startswith("$_DFF"))}


def jobs():
    """Every synthesis the study needs: (key, top, files, params)."""
    out = []
    for n in TLB_SIZES:
        for policy in (0, 1):
            out.append((f"tlb/{n}/{policy}", "sg_tlb", ["sg_tlb.sv"],
                        {"ENTRIES": n, "ASID_W": ASID_W, "POLICY": policy}))
    for n in WC_SIZES:
        out.append((f"wc/{n}", "sg_tagcache", ["sg_tagcache.sv"],
                    {"ENTRIES": n, "TAG_W": WC_TAG_W, "DATA_W": WC_DATA_W}))
    for n in CFG_SIZES:
        out.append((f"cfg/{n}", "sg_tagcache", ["sg_tagcache.sv"],
                    {"ENTRIES": n, "TAG_W": CFG_TAG_W, "DATA_W": CFG_DATA_W}))
    # whole unit: the default, and three more to check that the parts add up
    for tlb, wc, cfg, policy in ((8, 4, 4, 0), (2, 1, 1, 1), (16, 8, 2, 1), (32, 16, 8, 0)):
        out.append((f"unit/{tlb}/{wc}/{cfg}/{policy}", "streamguard", ALL_RTL,
                    {"TLB_ENTRIES": tlb, "WC_ENTRIES": wc, "CFG_ENTRIES": cfg,
                     "TLB_POLICY": policy}))
    return out


def load(rerun=False, workers=2):
    results = {} if rerun or not CACHE.exists() else json.loads(CACHE.read_text())
    todo = [j for j in jobs() if j[0] not in results]
    if todo:
        print(f"synthesising {len(todo)} configurations ...", file=sys.stderr)

        def run(job):
            key, top, files, params = job
            r = synth(top, files, params, key)
            print(f"  {key:22s} {r['ge']:10.0f} GE  {r['ffs']:5d} flip-flops", file=sys.stderr)
            return key, r

        CACHE.parent.mkdir(exist_ok=True)
        with ThreadPoolExecutor(workers) as pool:
            for key, r in pool.map(run, todo):
                results[key] = r
                CACHE.write_text(json.dumps(results, indent=1, sort_keys=True))
    return results


class AreaModel:
    def __init__(self, results):
        self.r = results
        default = results["unit/8/4/4/0"]["ge"]
        self.rest = default - (results["tlb/8/0"]["ge"] + results["wc/4"]["ge"]
                               + results["cfg/4"]["ge"])

    def caches(self, c):
        return (self.r[f"tlb/{c.tlb}/{c.policy}"]["ge"] + self.r[f"wc/{c.wc}"]["ge"]
                + self.r[f"cfg/{c.cfg}"]["ge"])

    def total(self, c):
        return self.rest + self.caches(c)

    def check(self):
        """Sum of parts against whole-unit synthesis, for the other three units."""
        from cachemodel import Config
        rows = []
        for key, r in self.r.items():
            if key.startswith("unit/") and key != "unit/8/4/4/0":
                tlb, wc, cfg, policy = map(int, key.split("/")[1:])
                c = Config(tlb, wc, cfg, policy)
                rows.append((c, r["ge"], self.total(c)))
        return sorted(rows, key=lambda row: row[1])


if __name__ == "__main__":
    res = load(rerun="--rerun" in sys.argv)
    model = AreaModel(res)
    print(f"rest of the unit: {model.rest:.0f} GE")
    for c, synth_ge, sum_ge in model.check():
        print(f"{c}: synthesised {synth_ge:.0f} GE, sum of parts {sum_ge:.0f} GE "
              f"({100 * (sum_ge - synth_ge) / synth_ge:+.1f}%)")
