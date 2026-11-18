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

Later milestones add functional coverage and formal proofs of the handshake
and isolation properties.

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

## 6. Assertions

| Name | Property |
|---|---|
| `u_chk_m_aw`, `u_chk_m_ar`, `u_chk_m_w` | VALID held and payload stable until READY, on every channel driven downstream |
| `u_chk_s_b`, `u_chk_s_r` | the same for responses driven to the device |
| `a_b_no_clash`, `a_r_no_clash` | a local error response never coincides with a downstream response |
| `a_wq_no_overflow` | the W decision queue never overflows |
| `a_tlb_single_hit`, `a_cache_single_hit` | no address matches more than one cache entry |

## 7. Do the tests catch bugs?

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

## 8. Results

| Milestone | Tests | Seeds | Result |
|---|---|---|---|
| 1 | 17 | 1 to 8 | all pass |
| 2 | 33 | 1 to 8 | all pass |
| 3 | 51 | 1 to 8 | all pass |
| 4 | 72 | 1 to 8 | all pass |

Synthesis check (Yosys, generic gates, default parameters):

| Milestone | Cells | Flip-flops |
|---|---|---|
| 1 | 825 | 266 |
| 2 | 2761 | 733 |
| 3 | 13322 | 2268 |
| 4 | 15364 | 2611 |

Most of the milestone 3 growth is cache storage: the TLB alone holds 8 entries
of about 90 bits.

## 9. Known gaps

- No functional coverage yet, so "which cases were hit" is not measured.
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
