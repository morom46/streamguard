# StreamGuard verification plan

## 1. Approach

- **Simulator:** Verilator, driven from Python with cocotb.
- **Stimulus:** an AXI4 requester on `s_axi`, an AXI4 memory on `m_axi`
  (both from cocotbext-axi), a small APB4 driver, random stalls on every
  channel on both sides.
- **Checking:**
  - a shadow copy of memory in the test, compared against the AXI memory
  - a monitor on the downstream address and data channels, to prove that
    terminated transactions never leave the unit
  - SystemVerilog assertions compiled into the design (`SG_ASSERT`)
- **Randomisation:** seeded through `COCOTB_RANDOM_SEED`; a failing seed
  reproduces exactly.

From milestone 2 every translated access is predicted by a Python reference
model (`tb/sg_model.py`) that reads the same memory the hardware reads. The
test compares the response, the fault code and level reported inside the
unit, the number of table reads, the downstream address and the data.
`tb/sg_pgtable.py` builds stream table entries, context descriptors and
translation tables, including deliberately faulty entries.

Two more things sit on top of the tests, both described in section 6:
functional coverage measures what the tests actually exercised, and formal
proofs show a few key properties for every possible input, not only the
simulated ones.

## 2. Milestone 1 features and tests

| Feature | Test | File |
|---|---|---|
| ID register values | `reset_values` | test_regs.py |
| Read-only registers ignore writes | `id_registers_are_read_only` | test_regs.py |
| CR0 write mask, CR0ACK follows CR0 | `cr0_write_mask_and_ack` | test_regs.py |
| APB byte strobes | `cr0_byte_strobes` | test_regs.py |
| GBPA changes only with UPDATE | `gbpa_needs_update_bit` | test_regs.py |
| Unmapped offsets RAZ/WI | `unmapped_offsets_raz_wi` | test_regs.py |
| Bypass: data and address unchanged | `bypass_write_then_read` | test_bypass.py |
| Bypass: 256-beat bursts | `bypass_long_bursts` | test_bypass.py |
| Stream table base registers keep only implemented bits | `strtab_base_registers` | test_regs.py |
| Bypass under stalls, 4 concurrent IDs | `bypass_random_traffic_with_backpressure` | test_bypass.py |
| W before AW is held until the decision | `write_data_before_address` | test_bypass.py |
| GBPA.ABORT terminates, nothing downstream | `gbpa_abort_terminates` (+ `_with_backpressure`) | test_bypass.py |
| SMMUEN=1 fails closed | `enabled_but_unconfigured_fails_closed` | test_bypass.py |
| Error B does not overtake an earlier B | `error_response_does_not_overtake_write` | test_bypass.py |
| Error R does not overtake an earlier R | `error_response_does_not_overtake_read` | test_bypass.py |
| Policy change during traffic, no hang, no partial effect | `policy_changes_under_traffic` | test_bypass.py |
| Reset returns to bypass | `reset_while_idle_recovers` | test_bypass.py |

## 3. Milestone 2 features and tests

All in test_translate.py.

| Feature | Test |
|---|---|
| STE abort, bypass, stage 1, invalid, unsupported Config, S1Fmt, StreamID out of range | `ste_configurations` |
| CD validity: V, AA64, TG0, ENDI, T0SZ range, EPD0 | `cd_checks` |
| 4 KB page, 2 MB block, 1 GB block; offset taken from the input address; ends of the 48-bit space | `page_and_block_sizes` |
| T0SZ selects start level and input range | `t0sz_sets_start_level_and_range` |
| Invalid descriptor at each level; reserved encodings at levels 0 and 3 | `translation_faults_at_every_level` |
| Access flag fault, AFFD | `access_flag_and_affd` |
| Read-only, privileged-only, UXN, PXN | `permissions` |
| Address size fault on leaf and table; fault priority | `address_size_faults` |
| Two streams, same input address, separate memory; no access to the other stream's pages | `streams_are_isolated` |
| Changed mapping used immediately (no stale state) | `table_updates_take_effect` |
| 256-beat burst with one translation | `burst_uses_one_translation` |
| Random tables with faulty entries, 6 streams, 500 accesses, each checked against the model | `random_tables_against_model` |
| The same with stalls on every channel including the table port | `random_tables_against_model_with_backpressure` |
| Four streams reading and writing at once, with stalls | `concurrent_streams` |
| Lookup latency recorded as the pre-TLB baseline | `lookup_latency_baseline` |

The random tests fail if they did not reach every fault type at least once.

## 4. Milestone 3 features and tests

The caches must never change the outcome of an access. The main check is
therefore the cached versions of the random tests: the same random tables and
accesses as milestone 2, with the caches on, mappings changed now and then
(each change followed by an invalidation, as software must do), and some
locality so that hits happen. Every access must still match the uncached
reference model exactly; only the number of table reads may be lower.
Directed tests in test_cache.py check the caches themselves.

| Feature | Test | File |
|---|---|---|
| Outcome unchanged by caching, with remaps and invalidations | `random_tables_against_model_cached` (+ `_with_backpressure`) | test_translate.py |
| Four streams at once with caches on | `concurrent_streams_cached` | test_translate.py |
| TLB hit needs no table reads; counters | `tlb_hit_skips_walk` | test_cache.py |
| Walk cache hit needs one read; configuration cache skips STE and CD | `walk_cache_and_config_cache` | test_cache.py |
| 2 MB and 1 GB entries cover the whole block, and only the block | `block_entries_cover_the_whole_block` | test_cache.py |
| Permissions checked on every hit; permission-faulting walks fill the TLB | `permissions_checked_on_hit` | test_cache.py |
| Translation, access flag and configuration faults are not cached | `faults_are_not_cached` | test_cache.py |
| Abort and bypass configurations are cached | `abort_and_bypass_configs_are_cached` | test_cache.py |
| Different ASIDs keep streams apart | `asid_keeps_streams_apart` | test_cache.py |
| Same ASID shares entries (architected behaviour) | `same_asid_shares_translations` | test_cache.py |
| Stale translation used until invalidation, then the new one | `stale_until_invalidated` | test_cache.py |
| Invalidation during a walk: no stale fill | `invalidate_during_walk` | test_cache.py |
| Disable: no hits, no fills, entries already present ignored | `caches_can_be_disabled` | test_cache.py |
| Counter clear leaves the caches intact | `counters_clear` | test_cache.py |
| Round robin replacement when full | `tlb_replacement` | test_cache.py |
| Hit, walk cache hit and miss latency | `hit_latency` | test_cache.py |
| SG_CTRL, SG_INV and counter registers | `cache_control_registers` | test_regs.py |

`invalidate_during_walk` holds the table read port after the level 2 read,
repoints the level 2 entry and invalidates, then releases. The access in
flight completes with the old page; the next access must see the new one.

## 5. Milestone 4 features and tests

Two checks run inside every translation test from earlier milestones:

- **Event queue after every access.** The reference model now also says
  whether a fault is recorded. After each access the bench reads the event
  queue: a recorded fault must have left exactly one record with the right
  event number, StreamID, address and access type; anything else must have
  left none.
- **Targeted invalidation in the cached random tests.** After changing a
  mapping the test picks, at random, one of the commands that is sufficient
  for that change (TLBI_NH_VA with either Leaf value and any address inside
  the mapping, TLBI_NH_VAA, TLBI_NH_ASID, TLBI_NH_ALL, TLBI_NSNH_ALL, or the
  SG_INV register). It also switches streams between translate and abort, and
  flips CD.R, each followed by a CFGI command. Every access must still match
  the model, so a command that invalidates too little is caught.

Directed tests, in test_queues.py unless noted:

| Feature | Test |
|---|---|
| Commands consumed in order across the queue wrap, every batch size | `commands_consumed_across_wrap` |
| Four-entry queues (LOG2SIZE = 2) for commands and events | `small_queues` |
| Nothing fetched while CMDQEN = 0 | `commands_wait_for_cmdqen` |
| TLBI_NH_VA: one page, Leaf 1 vs Leaf 0, other pages and ASIDs untouched | `tlbi_nh_va` |
| TLBI_NH_VA with an address inside a 2 MB or 1 GB block | `tlbi_nh_va_inside_a_block` |
| TLBI_NH_ASID and TLBI_NH_VAA | `tlbi_by_asid_and_by_address` |
| TLBI_NH_ALL, TLBI_NSNH_ALL leave the configuration cache | `tlbi_all_keeps_the_configuration` |
| Table change, TLBI, SYNC: old mapping before, new after | `stale_until_tlbi_and_sync` |
| CFGI_STE: one stream, TLB kept | `cfgi_ste` |
| CFGI_STE_RANGE and CFGI_ALL | `cfgi_range_and_all` |
| CFGI_CD, CFGI_CD_ALL | `cfgi_cd` |
| Illegal command: CONS and ERR, GERROR, interrupt, later commands held, resume after acknowledge, second error | `illegal_command_stops_the_queue` |
| A command does not start during a lookup | `command_waits_for_lookup` |
| Commands arriving non-stop during traffic from four streams | `commands_during_traffic` |
| Record contents for each kind of fault | `event_record_contents` |
| No record for abort or CD.R = 0, from a walk or a TLB hit | `faults_that_are_not_recorded` |
| EVENTQEN = 0 | `event_queue_disabled` |
| Overflow: records lost, flag set once, acknowledge, resume, second overflow | `event_queue_overflow` |
| Interrupt only on empty to not empty, and enable bit | `event_queue_interrupt` |
| Queue registers, pointer write protection | `queue_registers` (test_regs.py) |
| IRQ_CTRL, GERROR, GERRORN | `irq_and_error_registers` (test_regs.py) |

## 6. Milestone 5: functional coverage and formal proofs

### 6.1 Functional coverage

`tb/sg_coverage.py` defines 16 cover points with 104 bins. Each bin is one
situation that should be seen at least once. Samples come from two places:

- the reference model, once per bench access (what should happen)
- a probe on signals inside the design, every clock (what the hardware did)

| Cover point | Source | Bins |
|---|---|---|
| Lookup outcome: translated, bypass, abort, each fault type | model | 10 |
| Translated access: leaf size / read or write | model | 6 |
| Stage 1 access type: read or write, data or instruction, privileged or not | model | 6 |
| Fault type / level it was found at | model | 14 |
| Permission fault / access type | model | 5 |
| Terminated transaction: read or write, single beat or burst | model | 4 |
| Fault recording: each recorded event, CD.R = 0, STE abort | model | 9 |
| Cache path: configuration cached or fetched / TLB hit, walk cache hit, full walk | design | 6 |
| TLB hit: entry size / allowed or permission fault | design | 6 |
| Walk start level, including level 3 from the walk cache | design | 4 |
| Command executed: every opcode, and an illegal one | design | 13 |
| TLBI by address: command / Leaf | design | 4 |
| TLBI by address: size of the TLB entry removed, or none | design | 4 |
| CFGI: scope / whether an entry was removed | design | 6 |
| Queues: wrap of each queue, overflow, fault while disabled | design | 4 |
| Ordering: request while a command waits, command during a lookup, register invalidation during a lookup | design | 3 |

`make test` runs `tb/coverage_report.py` last. It writes `build/coverage.md`
(every bin with its hit count) and fails the regression if any bin is at zero,
so a test that silently stops exercising something is noticed.

**What the first run found.** 102 of 104 bins on one seed, and across seeds a
bin that was only hit by luck:

| Hole | Meaning | Closed by |
|---|---|---|
| TLB hit: 1 GB block / permission fault (2 MB only by chance) | The permission check on a TLB hit had never been run on a block entry in a directed test | block cases added to `permissions_checked_on_hit` |
| CFGI range / nothing cached, CFGI all / nothing cached | Invalidating an empty configuration cache was never tried | commands added at the start of `cfgi_range_and_all` |
| TLBI_NH_VAA with Leaf = 0 (random tests only) | No directed test used it | case added to `tlbi_by_asid_and_by_address` |

No design bug was behind these holes, but each was untested behaviour.
Result now: 104 of 104 on every seed tried (1 to 8 and 11 to 16).

**Not in the coverage model:** bus errors on the unit's own port (cannot be
produced by the memory model), the bypass datapath tests in test_bypass.py
(they do not use the bench that samples coverage), AXI burst types and sizes.

### 6.2 Formal proofs

`make formal` proves properties of six blocks (seven proofs: the TLB is proved
once for each replacement policy) with SymbiYosys and the Z3
solver, by k-induction: they hold from reset for any number of cycles and any
input sequence. The properties live at the end of each module under
`` `ifdef FORMAL``; the proof scripts are in `formal/`.

| Block | What is proved |
|---|---|
| `sg_perm` | The permission rule equals its case table for every combination of inputs |
| `sg_tagcache` | For any tag: a hit only if it was filled and not invalidated since; the data returned is the last data filled; no tag is stored twice; a fill is visible to the next lookup |
| `sg_tlb` | For any page of any address space: no hit after an invalidation that names it, until it is filled again, whatever size of entry covered it; the output keeps the offset inside the page or block; a fill is visible to the next lookup. Proved for round robin and for NRU, where it also proves that some entry is always not recently used and that the policy never picks one that is |
| `sg_addr_chan` | Nothing is sent downstream without a "forward" decision, and then only with the translated address and the device's own attributes; a request is terminated only after a "terminate" decision; one request at a time; VALID and payload stable until READY |
| `sg_memrd` | One address request per read; data accepted only after it; the words handed back are the ones the memory returned; ARVALID stable until ARREADY |
| `sg_evtq` | Nothing is written when the queue is disabled or full; one address then exactly four data beats with WLAST on the fourth; the pointer moves only after the memory accepted the record; at most one result per record; VALID and payload stable until READY |

How the cache proofs work: the proof picks one arbitrary tag (or page) that the
solver is free to choose, and keeps a small record of what the cache is allowed
to hold for it. An invariant ties every entry to that record. Because the
watched value is arbitrary, the result holds for all of them.

**Limits of these proofs.**

- They are block-level. Nothing is proved about the whole unit, for example
  that the lookup control only fills the TLB with what the walker returned.
- The TLB is proved with 4 entries (the design default is 8). With 8 the
  solver's run time became unpredictable (from two minutes to more than 15,
  depending on small changes to the proof); the logic is the same loop over
  entries.
- `sg_memrd` assumes its contract: the requester holds the address stable, and
  the memory returns RLAST on the last beat.
- `sg_addr_chan` is proved with narrow address and payload widths.
- No liveness: the proofs say nothing wrong happens, not that something
  eventually happens.

## 7. Milestone 6: second TLB policy and the sizing study

**The NRU policy.** `make -C tb PARAMS="TLB_POLICY=1"` builds the design with
NRU and runs the whole regression on it: all 73 tests pass and coverage is
104 of 104. `tlb_replacement` checks each policy's own choice: after every
entry is filled and the first half are used again, round robin replaces entry
0 and NRU replaces the first entry that was not used again. The `sg_tlb_nru`
proof covers it formally (section 6.2).

**The study's model.** The sizing study (docs/sizing_study.md) rests on a fast
Python model of the caches. `make validate` checks it against the RTL:
`tb/validate_model.py` replays every study workload through the RTL in four
cache configurations, one access at a time, and compares

- the hardware's hit and miss counters and its table reads with the model's,
  in total and access by access
- the measured translation time of every access with the model's latency
  formula, and that the fixed overhead of each lookup path is the same on
  every access
- the same again with every table read slowed by about 20 cycles, to check
  that the overheads do not depend on memory latency

| Configuration | Runs | Accesses | Result |
|---|---|---|---|
| TLB 8 round robin, walk cache 4, config cache 4 | 5 | 41,061 | equal |
| TLB 2 NRU, walk cache 1, config cache 1 | 5 | 41,061 | equal |
| TLB 16 NRU, walk cache 8, config cache 2 | 5 | 41,061 | equal |
| TLB 16 NRU, walk cache 8, config cache 2, slower memory | 5 | 41,061 | equal |
| TLB 32 round robin, walk cache 16, config cache 8 | 5 | 41,061 | equal |

Every one of the eight lookup paths appeared, each with one fixed overhead.

## 8. Assertions

| Name | Property |
|---|---|
| `u_chk_m_aw`, `u_chk_m_ar`, `u_chk_m_w` | VALID held and payload stable until READY, on every channel driven downstream |
| `u_chk_s_b`, `u_chk_s_r` | the same for responses driven to the device |
| `a_b_no_clash`, `a_r_no_clash` | a local error response never coincides with a downstream response |
| `a_wq_no_overflow` | the W decision queue never overflows |
| `a_tlb_single_hit`, `a_cache_single_hit` | no address matches more than one cache entry |

## 9. Do the tests catch bugs?

A regression that passes proves little unless it can fail. Each of these bugs
was put into the RTL, the regression run, and the change reverted.

| Injected bug | Caught by |
|---|---|
| Error B sent without waiting for outstanding writes | `a_b_no_clash`, `error_response_does_not_overtake_write` |
| W beats of a terminated burst forwarded downstream | `u_chk_m_w`, `gbpa_abort_terminates` |
| Enabled unit bypasses instead of terminating | `enabled_but_unconfigured_fails_closed` |
| Read error burst shorter than ARLEN+1 | `gbpa_abort_terminates` and four others |
| 2 MB block built with the 4 KB page offset | `page_and_block_sizes`, both random tests |
| Write to a read-only mapping not refused | `permissions`, both random tests, `concurrent_streams` |
| Access flag ignored | `access_flag_and_affd`, `address_size_faults`, both random tests |
| StreamID range not checked | `ste_configurations`, both random tests |
| Every stream uses stream table entry 0 | all 15 translation tests |
| Walk always starts at level 0 | `t0sz_sets_start_level_and_range`, both random tests |
| Address size fault checked after the access flag | `address_size_faults`, both random tests |
| Invalidation during a lookup does not stop its fill | `invalidate_during_walk` |
| TLB ignores the ASID | `asid_keeps_streams_apart`, `concurrent_streams_cached` |
| 2 MB entries compared as 4 KB pages | `block_entries_cover_the_whole_block`, both cached random tests |
| No permission check on a TLB hit | `permissions_checked_on_hit`, both cached random tests, `concurrent_streams_cached` |
| Invalidation does not clear the TLB | `stale_until_invalidated`, `same_asid_shares_translations` |
| Cache disable ignored by the TLB | `caches_can_be_disabled` (missed at first; the test gained the "disable after fill" case) |
| Cache disable ignored by the configuration cache | `caches_can_be_disabled` |
| Walks ending in any fault fill the TLB | `faults_are_not_cached`, both cached random tests |
| Walk cache looked up with the previous request's ASID | `concurrent_streams_cached` only |
| TLBI_NH_VA ignores the ASID | `tlbi_nh_va` |
| Leaf = 1 also drops the walk cache | `tlbi_nh_va` |
| Leaf = 0 keeps the walk cache | `tlbi_nh_va`, `command_waits_for_lookup` |
| TLBI compares block entries as 4 KB pages | `tlbi_nh_va_inside_a_block` |
| TLBI_NH_ASID leaves the walk cache | `tlbi_by_asid_and_by_address` |
| CFGI range off by one | `cfgi_range_and_all` |
| CFGI_CD does nothing | `cfgi_cd`, `cfgi_ste`, `cfgi_range_and_all`, both cached random tests |
| Unknown opcode treated as legal | `illegal_command_stops_the_queue` |
| Command starts without waiting for the lookup | `command_waits_for_lookup` |
| Request accepted while a command is waiting | `commands_during_traffic` (missed at first; this test was added for it) |
| CMDQ_CONS wrap ignores the queue size | `small_queues` |
| RnW inverted in the event record | `event_record_contents` and 16 others |
| Walk faults recorded whatever CD.R says | `faults_that_are_not_recorded`, both cached random tests |
| TLB-hit permission fault ignores CD.R | `faults_that_are_not_recorded`, both cached random tests |
| STE abort recorded as an event | `ste_configurations` and 7 others |
| Record written into a full queue | `event_queue_overflow` |
| Overflow flag toggles on every lost record | `event_queue_overflow` |
| Interrupt on every record | `event_queue_interrupt` |
| Event queue enable ignored | `event_queue_disabled`. The enable is checked in two places, so removing either check alone changes nothing; both had to be removed to see the failure |

The same was done for the formal proofs: each must fail when its block is
broken.

| Injected bug | Proof that fails |
|---|---|
| Unprivileged check removed from the permission rule | `sg_perm` |
| Selective invalidation does nothing | `sg_tagcache` |
| Fill ignores an existing entry with the same tag | `sg_tagcache` (duplicate tag) |
| Invalidation by address compares block entries as pages | `sg_tlb` |
| Fill wins over an invalidation in the same cycle | `sg_tlb` |
| 2 MB compare mask two bits short | `sg_tlb`. Passed at first: the proof used the design's own mask function, so it could not see a wrong mask. The proof now states "same page or block" separately |
| 1 GB compare mask one bit long | `sg_tlb` |
| NRU: all used bits may become set | `sg_tlb_nru` |
| NRU: victim ignores the used bits | `sg_tlb_nru` |
| NRU: selective invalidation does nothing | `sg_tlb_nru` (the shared properties hold for this policy too) |
| Request forwarded after a fault | `sg_addr_chan` |
| Untranslated address forwarded | `sg_addr_chan` |
| Second word stored over the first | `sg_memrd` |
| WLAST one beat early | `sg_evtq` |
| Record written into a full queue | `sg_evtq` |

## 10. Results

| Milestone | Tests | Seeds | Result |
|---|---|---|---|
| 1 | 17 | 1 to 8 | all pass |
| 2 | 33 | 1 to 8 | all pass |
| 3 | 51 | 1 to 8 | all pass |
| 4 | 72 | 1 to 8 | all pass |
| 5 | 73 | 1 to 8, 11 to 16 | all pass, 104 of 104 coverage bins on every seed |
| 6 | 73, with each TLB policy | 1 | all pass, 104 of 104 coverage bins with both policies |

Formal: 7 proofs pass by k-induction, about half a minute in total.

Sizing study model check (`make validate`): 25 runs, 205,305 accesses, hit
counters, table reads and translation cycles equal on every run.

Synthesis check (Yosys, generic gates, default parameters):

| Milestone | Cells | Flip-flops |
|---|---|---|
| 1 | 825 | 266 |
| 2 | 2761 | 733 |
| 3 | 13322 | 2268 |
| 4 | 15364 | 2611 |

Most of the milestone 3 growth is cache storage: the TLB alone holds 8 entries
of about 90 bits.

## 11. Known gaps

- Reset is only tested while idle, not during a burst.
- Narrow transfers, WRAP and FIXED bursts are forwarded untouched but not
  specifically tested.
- Downstream error responses (SLVERR/DECERR from memory) are passed through
  but not tested.
- External aborts on table reads (F_STE_FETCH, F_CD_FETCH, F_WALK_EABT) are
  implemented but not tested: the table memory model never returns an error.
- The reference model and the RTL were written from the same reading of the
  specification, so a shared misreading would not be caught. The spec
  cross-check table in architecture.md tracks this.
- No block-level testbench for the walker; it is tested through the top level.
- Cached random tests mostly miss: random addresses over large tables give a
  TLB hit rate around 10 percent. Hit paths are covered mainly by the directed
  tests. A realistic traffic pattern belongs to the sizing study.
- Only one replacement policy (round robin) and one set of cache sizes are
  tested.
- The walk cache ASID bug above is caught by only one test; a directed test
  with two ASIDs in the same 2 MB region would be stronger.
- Bus errors on the unit's own port are still untested: the memory model never
  returns an error. That leaves CERROR_ABT and GERROR.EVENTQ_ABT_ERR
  implemented but unexercised, along with the fetch aborts from milestone 2.
- The commands and the event layout come from the same reading of the
  specification as the tests. The new rows in the spec cross-check table list
  what has to be confirmed against the text.
- No test drives the queues the way a real driver would (Linux's
  arm-smmu-v3 probe and command sequences).
- LOG2SIZE above the maximum is capped in the RTL but not tested.
