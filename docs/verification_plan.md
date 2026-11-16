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

## 4. Assertions

| Name | Property |
|---|---|
| `u_chk_m_aw`, `u_chk_m_ar`, `u_chk_m_w` | VALID held and payload stable until READY, on every channel driven downstream |
| `u_chk_s_b`, `u_chk_s_r` | the same for responses driven to the device |
| `a_b_no_clash`, `a_r_no_clash` | a local error response never coincides with a downstream response |
| `a_wq_no_overflow` | the W decision queue never overflows |

## 5. Do the tests catch bugs?

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

## 6. Results

| Milestone | Tests | Seeds | Result |
|---|---|---|---|
| 1 | 17 | 1 to 8 | all pass |
| 2 | 33 | 1 to 8 | all pass |

Synthesis check (Yosys, generic gates, default parameters):

| Milestone | Cells | Flip-flops |
|---|---|---|
| 1 | 825 | 266 |
| 2 | 2761 | 733 |

## 7. Known gaps

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
