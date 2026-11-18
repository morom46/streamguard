"""Fast model of StreamGuard's three caches.

Replays a workload and reports, for every access, which path the lookup
took: configuration cached or fetched, TLB hit or miss, walk cache hit
or miss, and how many table reads it needed. Simulating the RTL takes
about a millisecond per access; this model takes about a microsecond,
which is what makes a sweep over hundreds of configurations possible.

It is only useful if it behaves exactly like the hardware, so it copies
the RTL's rules one for one (sg_xlate.sv, sg_tlb.sv, sg_tagcache.sv):

    * lookup order: configuration, then TLB, then walk cache, then walk
    * a walk starts at level 3 on a walk cache hit, otherwise at the
      level given by T0SZ, and reads one descriptor per level
    * the walk cache is filled when a walk passes a level 2 table entry
    * fills go to an entry with the same tag, else the lowest empty
      entry, else the policy's victim
    * round robin advances only when its victim is used
    * NRU: a fill or a used hit sets the entry's bit; when that would
      set every bit, only the new one is kept; the victim is the lowest
      entry whose bit is clear

tb/validate_model.py replays the same workloads through the RTL and
checks that the hardware's hit counters and table read count equal the
model's exactly.

Workloads only contain successful translations, so fault handling is
not modelled.

Latency. The time from a device's request to the forwarded request is
a fixed overhead that depends only on the lookup path, plus the time
spent waiting for table reads. The overheads below were measured on the
RTL (tb/validate_model.py), which also checks on every run that they
hold for every access and do not change when the memory is made slower:

    TLB hit                        4 cycles
    walk of n reads                6 + 4n cycles
    configuration fetched          9 cycles more, plus its two reads

A table read takes `read_latency` cycles from its address handshake to
its data. The STE and each descriptor are one beat; the CD is two beats,
so one cycle longer.
"""

from dataclasses import dataclass

LEVEL_SHIFT = {1: 30, 2: 21, 3: 12}
RR, NRU = 0, 1
POLICY_NAME = {RR: "round robin", NRU: "NRU"}


@dataclass(frozen=True)
class Config:
    tlb: int = 8
    wc: int = 4
    cfg: int = 4
    policy: int = RR

    def __str__(self):
        return f"TLB {self.tlb} {POLICY_NAME[self.policy]}, walk cache {self.wc}, config cache {self.cfg}"

    @property
    def params(self):
        """Verilog parameter overrides for this configuration."""
        return (f"TLB_ENTRIES={self.tlb} WC_ENTRIES={self.wc} "
                f"CFG_ENTRIES={self.cfg} TLB_POLICY={self.policy}")


class TagCache:
    """Exact-match cache: the configuration cache and the walk cache."""

    def __init__(self, entries):
        self.n = entries
        self.tags = [None] * entries       # None: invalid
        self.where = {}                    # tag -> index
        self.rr = 0

    def lookup(self, tag):
        return tag in self.where

    def fill(self, tag):
        if tag in self.where:
            return                          # same tag: overwrite in place
        if None in self.tags:
            i = self.tags.index(None)       # lowest empty entry
        else:
            i = self.rr
            self.rr = (self.rr + 1) % self.n
            del self.where[self.tags[i]]
        self.tags[i] = tag
        self.where[tag] = i


class Tlb:
    """Fully associative TLB holding 4 KB, 2 MB and 1 GB entries."""

    def __init__(self, entries, policy):
        self.n, self.policy = entries, policy
        self.key = [None] * entries        # (asid, level, va >> shift) or None
        self.where = {}                    # key -> index
        self.rr = 0
        self.used = [False] * entries
        self.n_used = 0

    @staticmethod
    def _key(asid, va, level):
        return (asid, level, va >> LEVEL_SHIFT[level])

    def lookup(self, asid, va):
        """Index of the entry covering va, or None. An entry of any size
        can cover it, so all three are tried."""
        for level in (3, 2, 1):
            i = self.where.get(self._key(asid, va, level))
            if i is not None:
                return i
        return None

    def _mark(self, i):
        if self.used[i]:
            return
        if self.n_used == self.n - 1:       # this would set every bit
            self.used = [False] * self.n
            self.n_used = 0
        self.used[i] = True
        self.n_used += 1

    def touch(self, i):
        if self.policy == NRU:
            self._mark(i)

    def fill(self, asid, va, level):
        key = self._key(asid, va, level)
        if key in self.where:
            i = self.where[key]
        elif None in self.key:
            i = self.key.index(None)
        else:
            if self.policy == NRU:
                # lowest entry not recently used (entry 0 if there is none,
                # which only happens with a single entry)
                i = self.used.index(False) if False in self.used else 0
            else:
                i = self.rr
                self.rr = (self.rr + 1) % self.n
            del self.where[self.key[i]]
        self.key[i] = key
        self.where[key] = i
        if self.policy == NRU:
            self._mark(i)


OVERHEAD_TLB_HIT = 4
OVERHEAD_WALK = 6
OVERHEAD_PER_WALK_READ = 4
OVERHEAD_CFG_FETCH = 9


def access_cycles(fetched, tlb_hit, walk, read_latency):
    """Translation time of one access, in cycles."""
    if tlb_hit:
        cycles = OVERHEAD_TLB_HIT
    else:
        cycles = OVERHEAD_WALK + walk * (OVERHEAD_PER_WALK_READ + read_latency)
    if fetched:
        cycles += OVERHEAD_CFG_FETCH + read_latency + (read_latency + 1)    # STE, CD
    return cycles


@dataclass
class Counters:
    accesses: int = 0
    tlb_hit: int = 0
    tlb_miss: int = 0
    wc_hit: int = 0
    cfg_hit: int = 0
    cfg_miss: int = 0
    reads: int = 0                  # table read requests
    walk_reads: int = 0
    # paths, for the latency model: (fetched, hit, walk) -> count
    paths: dict = None

    def cycles(self, read_latency):
        """Total translation time at a given table read latency."""
        return sum(n * access_cycles(*p, read_latency) for p, n in self.paths.items())

    def as_dict(self):
        return {k: v for k, v in self.__dict__.items() if k != "paths"}


def start_level(t0sz=16):
    return 0 if t0sz <= 24 else 1 if t0sz <= 33 else 2


def replay(workload, config, record=False):
    """Replay a workload. Returns the counters, and with record=True also
    one (cfg_fetched, tlb_hit, walk_reads) tuple per access."""
    cfgc = TagCache(config.cfg)
    tlb = Tlb(config.tlb, config.policy)
    wc = TagCache(config.wc)
    leaf = {sid: {va: level for va, _, level in maps} for sid, maps in workload.mappings.items()}

    def leaf_level(sid, va):
        for lv in (3, 2, 1):
            base = (va >> LEVEL_SHIFT[lv]) << LEVEL_SHIFT[lv]
            if leaf[sid].get(base) == lv:
                return lv
        raise KeyError(f"stream {sid}: {va:#x} is not mapped")
    first = start_level()
    c = Counters(paths={})
    trace = [] if record else None

    for sid, va, _write in workload.accesses:
        asid = workload.asid(sid)
        c.accesses += 1

        # configuration
        fetched = not cfgc.lookup(sid)
        if fetched:
            c.cfg_miss += 1
            c.reads += 2                     # STE, then CD (one 2-beat read)
            cfgc.fill(sid)
        else:
            c.cfg_hit += 1

        # TLB
        i = tlb.lookup(asid, va)
        if i is not None:
            c.tlb_hit += 1
            tlb.touch(i)
            walk = 0
        else:
            c.tlb_miss += 1
            level = leaf_level(sid, va)
            wc_tag = (asid, va >> 21)
            # A block's walk never passes a level 2 table entry, so the
            # walk cache never holds its region and cannot hit for it.
            if wc.lookup(wc_tag):
                c.wc_hit += 1
                walk = 1                     # start at level 3
            else:
                walk = level - first + 1     # one read per level
                if level == 3:
                    wc.fill(wc_tag)          # the walk passed a level 2 table entry
            tlb.fill(asid, va, level)
        c.walk_reads += walk
        c.reads += walk
        p = (fetched, i is not None, walk)
        c.paths[p] = c.paths.get(p, 0) + 1
        if record:
            trace.append((fetched, i is not None, walk))
    return (c, trace) if record else c
