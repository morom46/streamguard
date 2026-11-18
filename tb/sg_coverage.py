"""Functional coverage.

Passing tests say the checks held for the stimulus that was applied.
Coverage says what that stimulus was: which outcomes, fault types,
cache paths, commands and corner cases actually happened during the
regression.

A cover point is a named list of bins. A bin is one situation worth
seeing at least once. Two sources feed them:

    * the bench, for each access it makes: what the reference model
      predicted (sample_access)
    * a probe on signals inside the design, every clock: what the
      hardware did (Probe)

Everything collects into the module-level `cov` object, which lives for
the whole simulation. The last test module, coverage_report.py, writes
build/coverage.md and fails if any bin was never hit.
"""

from itertools import product

import sg_model as model

LEAF = {1: "1 GB block", 2: "2 MB block", 3: "4 KB page"}
FAULT = {
    model.FC_BAD_STREAMID: "C_BAD_STREAMID", model.FC_BAD_STE: "C_BAD_STE",
    model.FC_BAD_CD: "C_BAD_CD", model.FC_TRANSLATION: "F_TRANSLATION",
    model.FC_ADDR_SIZE: "F_ADDR_SIZE", model.FC_ACCESS: "F_ACCESS",
    model.FC_PERMISSION: "F_PERMISSION",
}
ACCESS = ["read data unprivileged", "read data privileged",
          "read instruction unprivileged", "read instruction privileged",
          "write unprivileged", "write privileged"]
COMMAND = {
    0x01: "CMD_PREFETCH_CONFIG", 0x02: "CMD_PREFETCH_ADDR", 0x03: "CMD_CFGI_STE",
    0x04: "CMD_CFGI_STE_RANGE", 0x05: "CMD_CFGI_CD", 0x06: "CMD_CFGI_CD_ALL",
    0x10: "CMD_TLBI_NH_ALL", 0x11: "CMD_TLBI_NH_ASID", 0x12: "CMD_TLBI_NH_VA",
    0x13: "CMD_TLBI_NH_VAA", 0x30: "CMD_TLBI_NSNH_ALL", 0x46: "CMD_SYNC",
}

# State encodings, in the order of the enums in sg_xlate.sv and sg_cmdq.sv
X_IDLE, X_LOOKUP, X_WALK_START, X_RSP, X_CMD = 0, 1, 8, 12, 13
C_EXEC = 3
K_S1 = 2


def access_bin(write, prot):
    who = "privileged" if prot & model.PROT_PRIVILEGED else "unprivileged"
    if write:
        return f"write {who}"
    what = "instruction" if prot & model.PROT_INSTRUCTION else "data"
    return f"read {what} {who}"


def cross(*lists):
    return [" / ".join(c) for c in product(*lists)]


class CoverPoint:
    def __init__(self, name, bins, source, note=""):
        self.name, self.source, self.note = name, source, note
        self.counts = {b: 0 for b in bins}

    def hit(self, bin_name):
        # a value outside the listed bins is a mistake in the coverage
        # model or a situation nobody planned for; either way, say so
        assert bin_name in self.counts, f"cover point '{self.name}': no bin '{bin_name}'"
        self.counts[bin_name] += 1

    @property
    def holes(self):
        return [b for b, n in self.counts.items() if n == 0]


class Coverage:
    def __init__(self):
        self.points = {}
        p = self._point
        # ---- from the reference model, one sample per bench access ----
        p("lookup outcome",
          ["translated", "bypass", "abort"] + list(FAULT.values()), "model")
        p("translated: leaf size / direction",
          cross(LEAF.values(), ["read", "write"]), "model")
        p("stage 1 access type", ACCESS, "model")
        p("fault / level",
          cross(["F_TRANSLATION", "F_ADDR_SIZE"], ["level 0", "level 1", "level 2", "level 3"])
          + cross(["F_ACCESS", "F_PERMISSION"], ["level 1", "level 2", "level 3"]), "model",
          "Access flag and permission faults need a leaf, so they cannot occur at level 0.")
        p("permission fault / access type",
          [a for a in ACCESS if a != "read data privileged"], "model",
          "A privileged data read can never be denied.")
        p("terminated transaction",
          cross(["read", "write"], ["single beat", "burst"]), "model")
        p("fault recording",
          [f"recorded {n}" for n in FAULT.values()]
          + ["not recorded: CD.R = 0", "not recorded: STE abort"], "model")
        # ---- from signals inside the design ----
        p("cache path",
          cross(["configuration cached", "configuration fetched"],
                ["TLB hit", "walk cache hit", "full walk"]), "design")
        p("TLB hit: entry size / result",
          cross(LEAF.values(), ["allowed", "permission fault"]), "design")
        p("walk start level",
          ["level 0", "level 1", "level 2", "level 3 (walk cache)"], "design")
        p("command executed", list(COMMAND.values()) + ["illegal opcode"], "design")
        p("TLBI by address: command / Leaf",
          cross(["CMD_TLBI_NH_VA", "CMD_TLBI_NH_VAA"], ["Leaf = 0", "Leaf = 1"]), "design")
        p("TLBI by address: TLB entry removed",
          ["none"] + list(LEAF.values()), "design")
        p("CFGI: scope / effect",
          cross(["one stream", "range", "all"], ["entry removed", "nothing cached"]), "design")
        p("queues",
          ["command queue wrapped", "event queue wrapped", "event queue overflow",
           "fault while event queue disabled"], "design")
        p("ordering",
          ["request arrives while a command waits", "command arrives during a lookup",
           "register invalidation during a lookup"], "design")

    def _point(self, name, bins, source, note=""):
        self.points[name] = CoverPoint(name, bins, source, note)

    def hit(self, point, bin_name):
        self.points[point].hit(bin_name)

    # ------------------------------------------------------------------
    def sample_access(self, exp, write, prot, va, length):
        """One bench access, as predicted by the reference model."""
        direction = "write" if write else "read"
        if exp.kind == "global":
            return                                   # SMMUEN = 0, not a lookup
        if not exp.fault:
            self.hit("lookup outcome", "translated" if exp.kind == "stage1" else "bypass")
        elif exp.fcode == model.FC_NONE:
            self.hit("lookup outcome", "abort")
        else:
            self.hit("lookup outcome", FAULT[exp.fcode])

        if exp.kind == "stage1":
            self.hit("stage 1 access type", access_bin(write, prot))
            if not exp.fault:
                self.hit("translated: leaf size / direction", f"{LEAF[exp.level]} / {direction}")
            elif exp.fcode in (model.FC_TRANSLATION, model.FC_ADDR_SIZE,
                               model.FC_ACCESS, model.FC_PERMISSION):
                self.hit("fault / level", f"{FAULT[exp.fcode]} / level {exp.level}")
            if exp.fcode == model.FC_PERMISSION:
                self.hit("permission fault / access type", access_bin(write, prot))

        if exp.fault:
            beats = ((va & 7) + length + 7) // 8
            self.hit("terminated transaction",
                     f"{direction} / {'single beat' if beats == 1 else 'burst'}")
            if exp.event:
                self.hit("fault recording", f"recorded {FAULT[exp.fcode]}")
            elif exp.fcode == model.FC_NONE:
                self.hit("fault recording", "not recorded: STE abort")
            else:
                self.hit("fault recording", "not recorded: CD.R = 0")

    # ------------------------------------------------------------------
    @property
    def totals(self):
        bins = sum(len(p.counts) for p in self.points.values())
        holes = sum(len(p.holes) for p in self.points.values())
        return bins, bins - holes

    def holes(self):
        return [(p.name, b) for p in self.points.values() for b in p.holes]

    def report(self):
        bins, hit = self.totals
        out = ["# Functional coverage", "",
               f"{hit} of {bins} bins hit, {len(self.points)} cover points.", "",
               "| Cover point | Source | Bins | Hit |", "|---|---|---|---|"]
        for p in self.points.values():
            out.append(f"| {p.name} | {p.source} | {len(p.counts)} | "
                       f"{len(p.counts) - len(p.holes)} |")
        for p in self.points.values():
            out += ["", f"## {p.name}", ""]
            if p.note:
                out += [p.note, ""]
            out += ["| Bin | Hits |", "|---|---|"]
            out += [f"| {b} | {n if n else '**0**'} |" for b, n in p.counts.items()]
        return "\n".join(out) + "\n"


cov = Coverage()


class Probe:
    """Samples signals inside the design. Call sample() once per clock."""

    def __init__(self, dut, coverage=cov):
        self.cov = coverage
        x = dut.u_xlate
        self.x, self.cmdq, self.evtq = x, x.u_cmdq, x.u_evtq
        self.tlb, self.cfgc, self.regs = x.u_tlb, x.u_cfgc, dut.u_regs
        self.tlb_entries = len(x.u_tlb.v_q)
        self.prev_cons = self.prev_prod = 0

    @staticmethod
    def _levels(packed, entries):
        return [(packed >> (2 * i)) & 3 for i in range(entries)]

    def sample(self):
        x, hit = self.x, self.cov.hit
        state = int(x.state_q.value)
        cmd_want = int(x.cmd_want.value)

        # ---- lookups ----
        if state == X_LOOKUP and int(x.eff_valid.value) and int(x.eff_kind.value) == K_S1:
            cfg = "configuration fetched" if int(x.cfg_have_q.value) else "configuration cached"
            if int(x.tlb_hit.value):
                hit("cache path", f"{cfg} / TLB hit")
                result = "permission fault" if int(x.tlb_denied.value) else "allowed"
                hit("TLB hit: entry size / result", f"{LEAF[int(x.tlb_level.value)]} / {result}")
            else:
                hit("cache path", f"{cfg} / {'walk cache hit' if int(x.wc_hit.value) else 'full walk'}")
        elif state == X_WALK_START:
            if int(x.wc_hit_q.value):
                hit("walk start level", "level 3 (walk cache)")
            else:
                t0sz = int(x.cur_t0sz_q.value)
                hit("walk start level", f"level {0 if t0sz <= 24 else 1 if t0sz <= 33 else 2}")
        elif state == X_RSP:
            if (int(x.rsp_fault.value) and int(x.rsp_fcode.value)
                    and not int(x.eventqen_i.value)):
                hit("queues", "fault while event queue disabled")

        # ---- ordering ----
        if cmd_want:
            if state == X_IDLE:
                if int(x.req_valid.value):
                    hit("ordering", "request arrives while a command waits")
            elif state != X_CMD:
                hit("ordering", "command arrives during a lookup")
        if int(x.inv_pend_q.value):
            hit("ordering", "register invalidation during a lookup")

        # ---- commands ----
        if int(self.cmdq.state_q.value) == C_EXEC:
            self._command()

        # ---- queues ----
        if int(self.evtq.ovf_o.value):
            hit("queues", "event queue overflow")
        # an index that goes down while its queue is enabled has wrapped
        cons, prod = int(self.regs.cmdq_rd_idx_o.value), int(self.regs.evtq_wr_idx_o.value)
        if cons < self.prev_cons and int(x.cmdqen_i.value):
            hit("queues", "command queue wrapped")
        if prod < self.prev_prod and int(x.eventqen_i.value):
            hit("queues", "event queue wrapped")
        self.prev_cons, self.prev_prod = cons, prod

    def _command(self):
        c, hit = self.cmdq, self.cov.hit
        opcode = int(c.cmd0_q.value) & 0xFF
        if not int(c.legal.value):
            hit("command executed", "illegal opcode")
            return
        name = COMMAND[opcode]
        hit("command executed", name)

        if int(c.tlb_inv_o.value) and int(c.inv_va_en_o.value):
            hit("TLBI by address: command / Leaf", f"{name} / Leaf = {int(c.leaf.value)}")
            removed = int(self.tlb.v_q.value) & int(self.tlb.inv_vec.value)
            levels = self._levels(int(self.tlb.lvl_q.value), self.tlb_entries)
            gone = {LEAF[levels[i]] for i in range(self.tlb_entries) if (removed >> i) & 1}
            for size in gone or {"none"}:
                hit("TLBI by address: TLB entry removed", size)

        if int(c.cfg_inv_o.value):
            mask = int(c.cfg_inv_mask_o.value)
            full = (1 << len(c.cfg_inv_mask_o)) - 1
            scope = "one stream" if mask == full else "all" if mask == 0 else "range"
            removed = int(self.cfgc.v_q.value) & int(self.cfgc.inv_vec.value)
            hit("CFGI: scope / effect",
                f"{scope} / {'entry removed' if removed else 'nothing cached'}")
