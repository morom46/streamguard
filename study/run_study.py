"""TLB sizing study: how big should StreamGuard's caches be?

Runs every combination of

    TLB entries          2, 4, 8, 16, 32, 64
    TLB policy           round robin, NRU
    walk cache entries   1, 2, 4, 8, 16
    config cache entries 1, 2, 4, 8

(240 configurations) over the workloads in workloads.py with the cache
model in cachemodel.py, takes the area of each from area.py, and writes

    docs/sizing_study.md       the report
    docs/img/*.png             its charts
    docs/sizing_sweep.csv      every configuration and its results

Run from the repository root with `make study`. Synthesis results are
cached in build/area.json; the first run synthesises (a few minutes).
"""

import csv
import itertools
import json
import random
import statistics
from functools import lru_cache
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt                    # noqa: E402

import area as A                                   # noqa: E402
import cachemodel as M                             # noqa: E402
import workloads as W                              # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DOCS, IMG = ROOT / "docs", ROOT / "docs" / "img"

READ_LATENCY = 100                 # cycles per table read assumed for the ranking
LATENCIES = [2, 20, 100]
DEFAULT = M.Config(8, 4, 4, M.RR)  # the configuration chosen in milestone 3
SEEDS = 30                         # runs per search method
SEARCH_BUDGET = 30                 # evaluations per search run (out of 240)

# colours: reference palette slots 1 and 2, grey for context
BLUE, ORANGE, GREY, INK, MUTED = "#2a78d6", "#eb6834", "#b9b8b3", "#0b0b0b", "#52514e"

WORKLOADS = W.build_all()
CONFIGS = [M.Config(t, w, c, p) for t, p, w, c in
           itertools.product(A.TLB_SIZES, (M.RR, M.NRU), A.WC_SIZES, A.CFG_SIZES)]


@lru_cache(maxsize=None)
def run(config, name):
    return M.replay(WORKLOADS[name], config)


def cycles(config, name, lat=READ_LATENCY):
    c = run(config, name)
    return c.cycles(lat) / c.accesses


def score(config, lat=READ_LATENCY):
    """Average translation time per access, combined over the main
    workloads with the geometric mean (as benchmark suites do), so that
    the slowest workload does not decide the result on its own."""
    return statistics.geometric_mean(cycles(config, n, lat) for n in W.MAIN)


def hit(config, name):
    c = run(config, name)
    return 100 * c.tlb_hit / c.accesses


def per_k(config, name, field="reads"):
    c = run(config, name)
    return 1000 * getattr(c, field) / c.accesses


def r_ffs(areas, c):
    """Flip-flops in the three caches of a configuration (counts add up exactly)."""
    r = areas.r
    return (r[f"tlb/{c.tlb}/{c.policy}"]["ffs"] + r[f"wc/{c.wc}"]["ffs"]
            + r[f"cfg/{c.cfg}"]["ffs"])


def load_validation():
    """Results written by `make validate`, one row per workload and configuration."""
    vfiles = sorted((ROOT / "build" / "validation").glob("*.jsonl"))
    return [json.loads(line) for f in vfiles for line in f.read_text().splitlines()]


def pareto(points):
    """Points (area, score, config) not beaten on both area and score."""
    front, best = [], float("inf")
    for a, s, c in sorted(points, key=lambda p: (p[0], p[1])):
        if s < best - 1e-9:
            front.append((a, s, c))
            best = s
    return front


# ---------------------------------------------------------------------
# Search: genetic algorithm and random search against the exhaustive optimum
# ---------------------------------------------------------------------
GENES = [A.TLB_SIZES, (M.RR, M.NRU), A.WC_SIZES, A.CFG_SIZES]


def decode(g):
    return M.Config(GENES[0][g[0]], GENES[2][g[2]], GENES[3][g[3]], GENES[1][g[1]])


def ga_search(fitness, budget, rng, pop_size=8, p_mut=0.3):
    """Small genetic algorithm: tournament selection, uniform crossover,
    per-gene mutation, one elite. Stops after `budget` distinct
    configurations have been evaluated."""
    seen = {}

    def f(g):
        g = tuple(g)
        if g not in seen and len(seen) < budget:
            seen[g] = fitness(decode(g))
        return seen.get(g, float("inf"))

    pop = [[rng.randrange(len(o)) for o in GENES] for _ in range(pop_size)]
    stall = 0
    while len(seen) < budget and stall < 200:
        before = len(seen)
        scored = sorted(pop, key=f)
        nxt = [scored[0]]
        while len(nxt) < pop_size:
            a, b = (min(rng.sample(scored, 2), key=f) for _ in range(2))
            child = [x if rng.random() < 0.5 else y for x, y in zip(a, b)]
            child = [rng.randrange(len(GENES[i])) if rng.random() < p_mut else v
                     for i, v in enumerate(child)]
            nxt.append(child)
        pop = nxt
        for g in pop:
            f(g)
        stall = stall + 1 if len(seen) == before else 0
    return min(seen.values())


def random_search(fitness, budget, rng):
    space = list(itertools.product(*(range(len(o)) for o in GENES)))
    return min(fitness(decode(g)) for g in rng.sample(space, budget))


# ---------------------------------------------------------------------
def main():
    IMG.mkdir(parents=True, exist_ok=True)
    areas = A.AreaModel(A.load())
    area = areas.total
    out = []                       # lines of the report

    def p(line=""):
        out.append(line)

    # ---- sweep -------------------------------------------------------
    rows = []
    for c in CONFIGS:
        rows.append({
            "tlb_entries": c.tlb, "tlb_policy": M.POLICY_NAME[c.policy],
            "walk_cache_entries": c.wc, "config_cache_entries": c.cfg,
            "area_ge": round(area(c)),
            **{f"score_r{lat}": round(score(c, lat), 2) for lat in LATENCIES},
            **{f"{n}_cycles_r{READ_LATENCY}": round(cycles(c, n), 2) for n in WORKLOADS},
            **{f"{n}_tlb_hit_pct": round(hit(c, n), 2) for n in WORKLOADS},
            **{f"{n}_reads_per_1000": round(per_k(c, n), 1) for n in WORKLOADS},
        })
    with open(DOCS / "sizing_sweep.csv", "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0]))
        wr.writeheader()
        wr.writerows(rows)

    points = [(area(c), score(c), c) for c in CONFIGS]
    front = pareto(points)
    budget = area(DEFAULT)
    feasible = [pt for pt in points if pt[0] <= budget]
    best_at_default = min(feasible, key=lambda pt: pt[1])
    best_overall = min(points, key=lambda pt: pt[1])
    # smallest configuration within 5% of the best score
    near = min((pt for pt in points if pt[1] <= best_overall[1] * 1.05), key=lambda pt: pt[0])

    # ---- report ------------------------------------------------------
    p("# StreamGuard TLB sizing study")
    p()
    p("How big should the TLB, walk cache and configuration cache be, and which")
    p("TLB replacement policy should be used? Milestone 3 picked 8 / 4 / 4 entries")
    p("with round robin without data. This study measures the choice.")
    p()
    p("Everything here is produced by `make study` (`study/run_study.py`); the")
    p("full results for all 240 configurations are in")
    p("[sizing_sweep.csv](sizing_sweep.csv) (`score_rN`: the score at a table read")
    p("latency of N cycles; `<workload>_cycles_r100`: average cycles per access).")
    p()
    p("## Summary")
    p()
    d_s, b_s = score(DEFAULT), best_at_default[1]
    bc = best_at_default[2]
    nru = M.Config(DEFAULT.tlb, DEFAULT.wc, DEFAULT.cfg, M.NRU)
    n_s = score(nru)
    nru_worse = [n for n in W.MAIN if cycles(nru, n) > cycles(DEFAULT, n) + 1e-9]

    def change_table(configs):
        p("  | Workload | " + " | ".join(str(c) for c in configs) + " |")
        p("  |---|" + "---|" * len(configs))
        for n in W.MAIN:
            base = cycles(configs[0], n)
            cells = [f"{base:.1f}"] + [f"{cycles(c, n):.1f} ({100 * (cycles(c, n) - base) / base:+.0f}%)"
                                       for c in configs[1:]]
            p(f"  | {n} | " + " | ".join(cells) + " |")
        p("  | **score** | " + " | ".join(f"**{score(c):.1f}**" for c in configs) + " |")
        p("  | area | " + " | ".join(f"{area(c) / 1000:.1f} kGE" for c in configs) + " |")
        p()

    p(f"- At a table read latency of {READ_LATENCY} cycles, the default configuration")
    p(f"  ({DEFAULT}) spends **{d_s:.1f} cycles per access** on translation")
    p(f"  (geometric mean over the four main workloads), for **{area(DEFAULT) / 1000:.1f} kGE**.")
    p(f"- **Keeping the sizes and switching the TLB to NRU** brings that to "
      f"**{n_s:.1f}** ({100 * (n_s - d_s) / d_s:+.0f}%),")
    p(f"  for {r_ffs(areas, nru) - r_ffs(areas, DEFAULT)} more flip-flops. "
      + ("No workload gets slower:" if not nru_worse else
         f"Slower on: {', '.join(nru_worse)}:"))
    p()
    change_table([DEFAULT, nru])
    p(f"- The best configuration within the default's area estimate is {bc}")
    p(f"  ({b_s:.1f}). It scores about the same as the policy change, but by")
    p("  giving up TLB entries for walk cache entries: packet_rx gains much more,")
    p("  multi_device loses. Area estimates are only good to about 10% (section 3), so")
    p("  these two are a tie on area too.")
    p()
    change_table([DEFAULT, bc])
    p(f"- The smallest configuration within 5% of the best result found is {near[2]},")
    p(f"  at {near[0] / 1000:.1f} kGE and {near[1]:.1f} cycles per access. The best result found")
    p(f"  is {best_overall[1]:.1f} cycles, for {best_overall[0] / 1000:.1f} kGE.")
    npu4, npu2m = run(M.Config(4, 4, 4, M.RR), "npu_conv"), run(M.Config(4, 4, 4, M.RR), "npu_conv_2m")
    npu64 = run(M.Config(64, 4, 4, M.RR), "npu_conv")
    p(f"- Mapping the NPU's buffers with 2 MB blocks instead of 4 KB pages does more")
    p(f"  than any TLB size: with 4 entries the hit rate goes from "
      f"{100 * npu4.tlb_hit / npu4.accesses:.1f}% to {100 * npu2m.tlb_hit / npu2m.accesses:.1f}%,")
    p(f"  where 64 entries of 4 KB pages reach {100 * npu64.tlb_hit / npu64.accesses:.1f}%.")
    vrows = load_validation()
    if vrows and all(r["match"] and r["translation_cycles"] == r["predicted_cycles"] for r in vrows):
        p("- The cache model used for the sweep was checked against the RTL: on every")
        p("  validation run the hardware's own hit counters, its table read count and its")
        p("  total translation time equal the model's exactly (section 2).")
    elif vrows:
        p("- **The cache model does not match the RTL on every validation run** (section 2).")
    else:
        p("- The cache model has not been checked against the RTL in this build: run")
        p("  `make validate`, then `make study` again.")
    p()

    # ---- method ------------------------------------------------------
    p("## 1. Method")
    p()
    p("1. **Workloads** (`study/workloads.py`): synthetic device traffic built from")
    p("   simple, documented rules, not recorded from real hardware.")
    p()
    p("   | Workload | Accesses | Pages touched | What it is |")
    p("   |---|---|---|---|")
    for n, w in WORKLOADS.items():
        tag = "" if n in W.MAIN else " (comparison only)"
        p(f"   | {n}{tag} | {len(w.accesses)} | {w.pages_touched} | {w.description} |")
    p()
    p("2. **Cache model** (`study/cachemodel.py`): replays a workload through")
    p("   copies of the RTL's cache rules, about a thousand times faster than")
    p("   simulating the RTL.")
    p("3. **Latency**: translation time per access is a fixed overhead that depends")
    p("   on the lookup path, plus the time spent waiting for table reads. The")
    p("   overheads were measured on the RTL:")
    p()
    p("   | Path | Overhead (cycles) | Table reads |")
    p("   |---|---|---|")
    p(f"   | TLB hit | {M.OVERHEAD_TLB_HIT} | 0 |")
    p(f"   | walk cache hit (walk of 1 read) | {M.OVERHEAD_WALK + M.OVERHEAD_PER_WALK_READ} | 1 |")
    p(f"   | full walk of a 4 KB page (4 reads) | {M.OVERHEAD_WALK + 4 * M.OVERHEAD_PER_WALK_READ} | 4 |")
    p(f"   | configuration not cached | +{M.OVERHEAD_CFG_FETCH} | +2 |")
    p()
    p(f"   The ranking assumes each table read takes **{READ_LATENCY} cycles**, roughly")
    p("   a DRAM access seen from a unit running near 1 GHz. The testbench memory")
    p("   answers in 2. Section 6 shows how the answer changes with this number.")
    p("4. **Area** (`study/area.py`): every cache size synthesised with Yosys and")
    p("   counted in NAND2 gate-equivalents (GE); section 3.")
    p("5. **Score**: average translation cycles per access for each of the four")
    p("   main workloads, combined with the geometric mean, as benchmark suites")
    p("   do: a 10% change counts the same on every workload, so the slowest one")
    p("   (packet_rx) does not decide the result alone. Lower is better. With the")
    p("   arithmetic mean the same configuration comes out best, but its advantage")
    p("   looks almost three times larger.")
    p()

    # ---- validation --------------------------------------------------
    p("## 2. Is the model right? Validation against the RTL")
    p()
    p("`make validate` builds the RTL in four cache configurations and replays")
    p("every workload through it, access by access, with all caches on. For each")
    p("run it compares the hardware's hit counters, its table reads and its total")
    p("translation time with the model's.")
    p()
    if vrows:
        p("| Configuration | Extra read latency | Workloads | Accesses | Counters and reads | Translation cycles |")
        p("|---|---|---|---|---|---|")
        groups = {}
        for r in vrows:
            groups.setdefault((r["config"], r["read_latency_extra"]), []).append(r)
        for (cfg, extra), rs in sorted(groups.items()):
            ok_c = all(r["match"] for r in rs)
            ok_t = all(r["translation_cycles"] == r["predicted_cycles"] for r in rs)
            p(f"| {cfg} | {extra} | {len(rs)} | {sum(r['accesses'] for r in rs)} | "
              f"{'equal' if ok_c else 'DIFFERENT'} | {'equal' if ok_t else 'DIFFERENT'} |")
        p()
        paths = {}
        for r in vrows:
            for k, v in r["overhead_by_path"].items():
                paths.setdefault(k, set()).update(v)
        p(f"In total {sum(r['accesses'] for r in vrows)} accesses. Every lookup path")
        p("had the same overhead on every access, at both read latencies:")
        p()
        p("| Path | Overhead (cycles) |")
        p("|---|---|")
        for k, v in sorted(paths.items()):
            p(f"| {k} | {', '.join(map(str, sorted(v)))} |")
        p()
    else:
        p("*No validation results found: run `make validate` before `make study`.*")
        p()
    p("What this does and does not show: the model reproduces the hardware's cache")
    p("behaviour and timing for these workloads and these configurations, exactly.")
    p("It does not show that the workloads are realistic, or that the timing holds")
    p("with a memory whose latency varies from read to read.")
    p()

    # ---- area --------------------------------------------------------
    r = areas.r
    p("## 3. Area")
    p()
    p("Each cache synthesised on its own (Yosys, generic NAND/NOR/inverter gates and")
    p("flip-flops), converted to NAND2 gate-equivalents: NAND2 and NOR2 1.0,")
    p(f"inverter {A.GE_NOT}, flip-flop with reset {A.GE_FF_RESET}, clock enable +{A.GE_ENABLE}.")
    p("These ratios are typical of standard-cell libraries but belong to none in")
    p("particular: good for comparing configurations, not for predicting silicon.")
    p()
    p("| Entries | TLB, round robin | TLB, NRU | Walk cache | Config cache |")
    p("|---|---|---|---|---|")
    for n in sorted(set(A.TLB_SIZES) | set(A.WC_SIZES) | set(A.CFG_SIZES)):
        cell = lambda k: f"{r[k]['ge'] / 1000:.1f} kGE ({r[k]['ffs']} FF)" if k in r else ""  # noqa: E731
        p(f"| {n} | {cell(f'tlb/{n}/0')} | {cell(f'tlb/{n}/1')} | {cell(f'wc/{n}')} | {cell(f'cfg/{n}')} |")
    p()
    p(f"The rest of the unit is {areas.rest / 1000:.1f} kGE. A TLB entry costs about")
    p(f"{r['tlb/64/0']['ge'] / 64 / 1000:.1f} kGE: 36 bits of virtual tag, 36 of physical")
    p("address, ASID, level, permissions, and a comparator. The NRU policy adds one")
    p("flip-flop per entry; the difference is smaller than the variation between")
    p("synthesis runs (the 4 and 16 entry NRU TLBs even come out smaller).")
    p()
    p("Configuration area = rest of the unit + the three caches. Checked against")
    p("synthesising the whole unit:")
    p()
    p("| Configuration | Whole unit synthesised | Sum of parts | Difference |")
    p("|---|---|---|---|")
    for c, s_ge, sum_ge in areas.check():
        p(f"| {c} | {s_ge / 1000:.1f} kGE | {sum_ge / 1000:.1f} kGE | {100 * (sum_ge - s_ge) / s_ge:+.1f}% |")
    p()
    p("Flip-flop counts add up exactly; the gate counts differ by up to about 10%")
    p("because synthesis optimises across module boundaries differently in each run.")
    p("Area differences below 10% between configurations are not significant.")
    p()

    # ---- TLB size and policy -----------------------------------------
    p("## 4. TLB size and replacement policy")
    p()
    p("TLB hit rate, walk cache 4 and configuration cache 4 entries:")
    p()
    p("| Entries | " + " | ".join(f"{n} RR | {n} NRU" for n in W.MAIN) + " |")
    p("|---|" + "---|---|" * len(W.MAIN))
    for t in A.TLB_SIZES:
        cells = []
        for n in W.MAIN:
            for pol in (M.RR, M.NRU):
                cells.append(f"{hit(M.Config(t, 4, 4, pol), n):.1f}%")
        p(f"| {t} | " + " | ".join(cells) + " |")
    p()
    p("![TLB hit rate against TLB entries](img/tlb_hit_rate.png)")
    p()
    dma = run(M.Config(64, 4, 4, M.RR), "dma_copy")
    p("What the table shows:")
    p()
    p(f"- **dma_copy** is {100 * dma.tlb_hit / dma.accesses:.1f}% at every size: each 4 KB")
    p("  page is used for 16 bursts and never again, so the only misses are the")
    p("  first touch of each page. A bigger TLB cannot help a stream; only bigger")
    p("  pages or prefetching can.")
    p(f"- **npu_conv** jumps between 8 and 16 entries. Every output row re-reads all")
    p("  9 weight pages plus 3 or 4 input pages and an output page: about 14 pages in")
    p("  a loop. With 8 entries the loop does not fit and the weights miss every row;")
    p("  with 16 it fits. Neither policy can help a loop that is larger than the TLB.")
    p(f"- **packet_rx** is where the policy matters. Each packet lands on a new")
    p("  page, but the descriptor ring is used for every packet. Round robin evicts")
    p("  the ring's page in turn with everything else; NRU keeps it because it is")
    p(f"  hit constantly. With 2 entries NRU reaches {hit(M.Config(2, 4, 4, M.NRU), 'packet_rx'):.1f}% against "
      f"{hit(M.Config(2, 4, 4, M.RR), 'packet_rx'):.1f}%")
    p(f"  for round robin, more than round robin reaches with 32 entries "
      f"({hit(M.Config(32, 4, 4, M.RR), 'packet_rx'):.1f}%).")
    p("- **multi_device** needs about 8 entries for four devices; above that the")
    p("  misses are mostly first touches of the camera and USB buffers.")
    p()
    worst = max(((100 * (cycles(M.Config(t, w, cf, M.NRU), n) - cycles(M.Config(t, w, cf, M.RR), n))
                  / cycles(M.Config(t, w, cf, M.RR), n), n, M.Config(t, w, cf, M.NRU))
                 for t in A.TLB_SIZES for w in A.WC_SIZES for cf in A.CFG_SIZES for n in W.MAIN),
                key=lambda x: x[0])
    small = [t for t in A.TLB_SIZES if any(
        cycles(M.Config(t, w, cf, M.NRU), n) > cycles(M.Config(t, w, cf, M.RR), n) + 1e-9
        for w in A.WC_SIZES for cf in A.CFG_SIZES for n in W.MAIN)]
    p("NRU is not always better. Over all 240 configurations and four workloads,")
    p(f"its worst result against round robin is {worst[0]:+.0f}% translation time, for")
    p(f"{worst[1]} with {worst[2]}. With only four entries for four devices taking")
    p("turns, a one-bit history is coarse: once every entry has been used the bits")
    p("are cleared and the choice falls to the lowest-numbered entry, which is the")
    p(f"likely reason. NRU loses somewhere only with {', '.join(map(str, small))} TLB entries;")
    p("with more it is equal or better on every workload and configuration.")
    p()

    # ---- walk cache ----------------------------------------------------
    p("## 5. Walk cache and configuration cache")
    p()
    p("A walk cache entry remembers where one 2 MB region's level 3 table is, so a")
    p("TLB miss in that region needs 1 table read instead of 4.")
    p()
    p(f"packet_rx, TLB 8 NRU, configuration cache 4, read latency {READ_LATENCY}:")
    p()
    p("| Walk cache entries | Walk cache hits per TLB miss | Table reads per 1000 accesses | Cycles per access |")
    p("|---|---|---|---|")
    for wc in A.WC_SIZES:
        c = M.Config(8, wc, 4, M.NRU)
        cn = run(c, "packet_rx")
        p(f"| {wc} | {100 * cn.wc_hit / max(cn.tlb_miss, 1):.0f}% | {per_k(c, 'packet_rx'):.0f} | "
          f"{cycles(c, 'packet_rx'):.1f} |")
    p()
    p("The packet buffers are spread over 16 regions of 2 MB, so 16 walk cache")
    p("entries cover all of them and most TLB misses cost one read. Every")
    p("doubling helps on this workload; on dma_copy and npu_conv, whose buffers")
    p("sit in two 2 MB regions each, two entries are enough.")
    p()
    p(f"multi_device, TLB 8 NRU, walk cache 4, read latency {READ_LATENCY}:")
    p()
    p("| Config cache entries | Configuration misses per 1000 accesses | Cycles per access |")
    p("|---|---|---|")
    for cf in A.CFG_SIZES:
        c = M.Config(8, 4, cf, M.NRU)
        p(f"| {cf} | {per_k(c, 'multi_device', 'cfg_miss'):.0f} | {cycles(c, 'multi_device'):.1f} |")
    p()
    p("Four devices take turns. With fewer configuration entries than devices, round")
    p("robin replacement evicts each device's configuration just before it is")
    p("needed again, and every switch costs two extra table reads. One entry per")
    p("active device removes this completely; more do nothing.")
    p()

    # ---- latency ---------------------------------------------------------
    p("## 6. How much does memory latency matter?")
    p()
    p("Average cycles per access for the default configuration and the best")
    p("configuration at the same area, at three table read latencies:")
    p()
    p("| Workload | " + " | ".join(f"default, {lat}" for lat in LATENCIES) + " | "
      + " | ".join(f"best, {lat}" for lat in LATENCIES) + " |")
    p("|---|" + "---|" * (2 * len(LATENCIES)))
    for n in list(W.MAIN) + ["npu_conv_2m"]:
        p(f"| {n} | " + " | ".join(f"{cycles(DEFAULT, n, lat):.1f}" for lat in LATENCIES) + " | "
          + " | ".join(f"{cycles(best_at_default[2], n, lat):.1f}" for lat in LATENCIES) + " |")
    p()
    worst2 = max(cycles(c, n, 2) for c in (DEFAULT, best_at_default[2]) for n in W.MAIN)
    p(f"With a 2-cycle memory a miss costs little: no workload averages more than")
    p(f"{worst2:.1f} cycles per access. At DRAM-like latency the misses dominate, and")
    p("packet_rx in particular spends most of its translation time waiting for table")
    p("reads. The best configuration within the default area at other latencies:")
    p()
    for lat in (20, 2):
        best_lat = min((pt for pt in points if pt[0] <= budget), key=lambda pt: score(pt[2], lat))
        p(f"- at {lat} cycles the best within the default area is {best_lat[2]}"
          + (" (the same)." if best_lat[2] == best_at_default[2] else "."))
    p()

    # ---- pareto ------------------------------------------------------------
    p("## 7. Area against performance")
    p()
    p("![Area against translation time for all 240 configurations](img/pareto.png)")
    p()
    p("Every point is one configuration. The blue line joins the configurations")
    p("that no other configuration beats on both area and time (the Pareto front):")
    p()
    p("| Configuration | Area (kGE) | Cycles per access |")
    p("|---|---|---|")
    for a, s, c in front:
        mark = " (default)" if c == DEFAULT else ""
        p(f"| {c}{mark} | {a / 1000:.1f} | {s:.1f} |")
    p()
    on_front = any(c == DEFAULT for _, _, c in front)
    p(f"The default configuration is {'on' if on_front else 'not on'} the front.")
    p(f"Within its area ({budget / 1000:.1f} kGE) the best choice is {best_at_default[2]}.")
    p()
    p("Recommendation, for these workloads and assumptions:")
    p()
    p(f"- **Switch the TLB to NRU and keep the sizes** ({nru}). It is "
      f"{100 * (d_s - n_s) / d_s:.0f}% better")
    p("  overall, " + ("no workload gets slower" if not nru_worse else "a few workloads get slower")
      + f", and it costs {r_ffs(areas, nru) - r_ffs(areas, DEFAULT)} flip-flops.")
    p(f"- If network-like traffic matters most, {bc} is the alternative at the")
    p("  same area: much better for packet_rx, worse when many devices share the unit.")
    p(f"- With NRU, keep more than {max(small)} TLB entries: at {max(small)} it can lose to round robin.")
    p("- Give the configuration cache one entry per active device; more do nothing.")
    p("- If software can map device buffers with **2 MB blocks**, a much smaller")
    p("  TLB is enough.")
    p()

    # ---- search ------------------------------------------------------------
    p("## 8. Searching instead of trying everything")
    p()
    p("With a model that evaluates a configuration in a fraction of a second, all")
    p("240 can simply be tried, and that is what the results above come from. In a")
    p("real flow each candidate would be checked by simulating and synthesising the")
    p("RTL, which here takes about a minute per configuration, and the space would")
    p("be larger (associativity, separate TLBs per page size, more policies). Then")
    p("the number of evaluations matters.")
    p()
    p(f"The exhaustive result gives the true optimum, so search methods can be")
    p(f"measured against it. The task: best score within the default's area, with")
    p(f"{SEARCH_BUDGET} evaluations (one eighth of the space), {SEEDS} runs per method.")
    p()

    def fitness(c):
        a = area(c)
        return score(c) if a <= budget else 1e6 + a

    optimum = best_at_default[1]
    res = {}
    for name, method in (("genetic algorithm", ga_search), ("random search", random_search)):
        found = [method(fitness, SEARCH_BUDGET, random.Random(seed)) for seed in range(SEEDS)]
        gaps = [100 * (f - optimum) / optimum for f in found]
        res[name] = (sum(g < 1e-9 for g in gaps), statistics.median(gaps), max(gaps))
    p("| Method | Runs that found the optimum | Median gap to optimum | Worst gap |")
    p("|---|---|---|---|")
    for name, (hits, med, worst) in res.items():
        p(f"| {name} | {hits} of {SEEDS} | {med:.1f}% | {worst:.1f}% |")
    p()
    p("The genetic algorithm is the same design as in")
    p("[FlashFit](https://github.com/morom46/flashfit): tournament")
    p("selection, uniform crossover, per-gene mutation, one elite, population 8.")
    p("It was not tuned for this problem. With a space this small the honest")
    p("conclusion is that exhaustive search is the right tool here; the comparison")
    p("shows how the methods would trade evaluations for certainty if it were not.")
    p()

    # ---- limits --------------------------------------------------------------
    p("## 9. Limits")
    p()
    p("- The workloads are synthetic. Their locality is chosen to resemble real")
    p("  devices, but no real trace was used. A different mix would change the")
    p("  weights of the conclusions, though the mechanisms (streams cannot be helped")
    p("  by capacity, loops need to fit, a hot page needs a policy that sees use)")
    p("  hold generally.")
    p("- One access at a time. The RTL handles one translation at a time, so the")
    p("  study measures latency per access; it says nothing about throughput with")
    p("  several requests in flight, which the RTL does not support.")
    p("- The read latency is a single number. Real memory latency varies, and")
    p("  table reads may hit in a system cache.")
    p("- Area is a gate-equivalent estimate from generic gates and flip-flops,")
    p("  without timing constraints, placement or a real cell library.")
    p("- Only fully associative caches and two policies were studied.")
    p()
    (DOCS / "sizing_study.md").write_text("\n".join(out) + "\n")

    # ---- charts --------------------------------------------------------------
    plt.rcParams.update({"font.size": 9, "axes.edgecolor": MUTED, "axes.labelcolor": INK,
                         "xtick.color": MUTED, "ytick.color": MUTED, "axes.titlecolor": INK,
                         "figure.facecolor": "#fcfcfb", "axes.facecolor": "#fcfcfb"})
    fig, axes = plt.subplots(1, 4, figsize=(11, 3), sharey=False)
    for ax, n in zip(axes, W.MAIN):
        for pol, colour, style in ((M.RR, BLUE, "-"), (M.NRU, ORANGE, "--")):
            ys = [hit(M.Config(t, 4, 4, pol), n) for t in A.TLB_SIZES]
            ax.plot(A.TLB_SIZES, ys, style, color=colour, lw=2, marker="o", ms=4,
                    label=M.POLICY_NAME[pol])
        ax.set_xscale("log", base=2)
        ax.set_xticks(A.TLB_SIZES, [str(t) for t in A.TLB_SIZES])
        ax.set_title(n, fontsize=10)
        ax.set_xlabel("TLB entries")
        ax.grid(axis="y", color="#e5e4df", lw=0.8)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
    axes[0].set_ylabel("TLB hit rate (%)")
    axes[0].legend(frameon=False, loc="lower right")
    fig.suptitle("TLB hit rate against size, walk cache 4, config cache 4", fontsize=10, color=INK)
    fig.tight_layout()
    fig.savefig(IMG / "tlb_hit_rate.png", dpi=150)
    plt.close(fig)

    fig, (ax, zoom) = plt.subplots(1, 2, figsize=(11, 4.4), gridspec_kw={"width_ratios": [1.3, 1]})
    nru_pt = points[CONFIGS.index(M.Config(DEFAULT.tlb, DEFAULT.wc, DEFAULT.cfg, M.NRU))]
    default_pt = points[CONFIGS.index(DEFAULT)]
    box = (30, 58, 15, 30)                               # zoomed area: x0, x1, y0, y1
    for axis in (ax, zoom):
        axis.scatter([a / 1000 for a, _, _ in points], [s for _, s, _ in points],
                     s=10, color=GREY, label="all configurations", zorder=1)
        axis.plot([a / 1000 for a, _, _ in front], [s for _, s, _ in front], "-o", color=BLUE,
                  lw=2, ms=4, label="Pareto front", zorder=2)
        axis.axvline(budget / 1000, color=MUTED, lw=1, ls=":")
        for pt, colour in ((default_pt, ORANGE), (nru_pt, BLUE), (best_at_default, BLUE)):
            axis.scatter([pt[0] / 1000], [pt[1]], s=60, color=colour, edgecolor="#fcfcfb",
                         linewidth=2, zorder=3)
        axis.grid(color="#e5e4df", lw=0.8)
        for side in ("top", "right"):
            axis.spines[side].set_visible(False)
        axis.set_xlabel("Area (kGE)")
    ax.add_patch(plt.Rectangle((box[0], box[2]), box[1] - box[0], box[3] - box[2],
                               fill=False, ec=MUTED, lw=1, ls="--", zorder=4))
    ax.text(budget / 1000 + 1.5, 62, "default area", color=MUTED, fontsize=8)
    ax.set_ylabel(f"Cycles per access (read latency {READ_LATENCY})")
    ax.set_title("All 240 configurations", fontsize=10)
    ax.legend(frameon=False)
    zoom.set_xlim(box[0], box[1])
    zoom.set_ylim(box[2], box[3])
    zoom.set_title("Around the default (dashed box)", fontsize=10)
    for pt, label, dx, dy, ha in ((default_pt, "default: TLB 8 round robin, WC 4, CC 4", 8, 10, "left"),
                                  (nru_pt, "same sizes with NRU (recommended)", 8, -14, "left"),
                                  (best_at_default, "TLB 4 NRU, WC 8, CC 4", -8, -14, "right")):
        zoom.annotate(label, (pt[0] / 1000, pt[1]), xytext=(dx, dy), textcoords="offset points",
                      ha=ha, color=INK, fontsize=8.5)
    zoom.text(box[0] + 0.5, box[2] + 0.5, "WC: walk cache entries, CC: config cache entries",
              color=MUTED, fontsize=8)
    fig.tight_layout()
    fig.savefig(IMG / "pareto.png", dpi=150)
    plt.close(fig)

    print(f"wrote {DOCS / 'sizing_study.md'}")


if __name__ == "__main__":
    main()
