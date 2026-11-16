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

Later milestones add a Python reference model of the translation, a random
page table generator, functional coverage, and formal proofs of the
handshake and isolation properties.

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
| Bypass under stalls, 4 concurrent IDs | `bypass_random_traffic_with_backpressure` | test_bypass.py |
| W before AW is held until the decision | `write_data_before_address` | test_bypass.py |
| GBPA.ABORT terminates, nothing downstream | `gbpa_abort_terminates` (+ `_with_backpressure`) | test_bypass.py |
| SMMUEN=1 fails closed | `enabled_without_translation_fails_closed` | test_bypass.py |
| Error B does not overtake an earlier B | `error_response_does_not_overtake_write` | test_bypass.py |
| Error R does not overtake an earlier R | `error_response_does_not_overtake_read` | test_bypass.py |
| Policy change during traffic, no hang, no partial effect | `policy_changes_under_traffic` | test_bypass.py |
| Reset returns to bypass | `reset_while_idle_recovers` | test_bypass.py |

## 3. Assertions

| Name | Property |
|---|---|
| `u_chk_m_aw`, `u_chk_m_ar`, `u_chk_m_w` | VALID held and payload stable until READY, on every channel driven downstream |
| `u_chk_s_b`, `u_chk_s_r` | the same for responses driven to the device |
| `a_b_no_clash`, `a_r_no_clash` | a local error response never coincides with a downstream response |
| `a_wq_no_overflow` | the W decision queue never overflows |

## 4. Do the tests catch bugs?

A regression that passes proves little unless it can fail. Each of these bugs
was put into the RTL, the regression run, and the change reverted.

| Injected bug | Caught by |
|---|---|
| Error B sent without waiting for outstanding writes | `a_b_no_clash`, `error_response_does_not_overtake_write` |
| W beats of a terminated burst forwarded downstream | `u_chk_m_w`, `gbpa_abort_terminates` |
| Enabled unit bypasses instead of terminating | `enabled_without_translation_fails_closed` |
| Read error burst shorter than ARLEN+1 | `gbpa_abort_terminates` and four others |

## 5. Results

| Milestone | Tests | Seeds | Result |
|---|---|---|---|
| 1 | 17 | 1 to 8 | all pass |

Synthesis check (Yosys, generic gates, default parameters): 825 cells, of
which 266 are flip-flops. The number is only a baseline for later milestones.

## 6. Known gaps

- No functional coverage yet, so "which cases were hit" is not measured.
- Reset is only tested while idle, not during a burst.
- Narrow transfers, WRAP and FIXED bursts are forwarded untouched but not
  specifically tested.
- Downstream error responses (SLVERR/DECERR from memory) are passed through
  but not tested.
