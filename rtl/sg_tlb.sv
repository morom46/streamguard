// StreamGuard - translation lookaside buffer.
//
// Fully associative cache of leaf translations. An entry holds one
// 4 KB page, 2 MB block or 1 GB block, tagged with the ASID of the
// address space it came from:
//
//   valid | ASID | level | VA[47:12] | PA[47:12] | AP[1], AP[2], PXN, UXN
//
// The level decides how many VA bits take part in the compare
// (level 1: VA[47:30], level 2: VA[47:21], level 3: VA[47:12]) and how
// many output address bits come from the entry instead of the input.
//
// Lookup is combinational. A fill writes, in order of preference, an
// entry that already holds the same translation, the first empty
// entry, or the round robin victim.
//
// Invalidation. inv_all clears every entry. inv_sel clears the entries
// selected by the command: those with inv_asid if inv_asid_en is set,
// and those covering inv_va if inv_va_en is set (with neither set,
// all of them). "Covering" uses the entry's own level, so one address
// inside a 2 MB or 1 GB block removes the block entry. Invalidation
// wins over a fill in the same cycle.
//
// Software must give different address spaces different ASIDs: two
// streams with the same ASID share TLB entries.

module sg_tlb #(
  parameter int unsigned ENTRIES = 8,
  parameter int unsigned ASID_W  = 8
) (
  input  logic              clk,
  input  logic              rst_n,

  input  logic              inv_all,
  input  logic              inv_sel,
  input  logic              inv_asid_en,
  input  logic [ASID_W-1:0] inv_asid,
  input  logic              inv_va_en,
  input  logic [47:12]      inv_va,

  // Lookup
  input  logic [ASID_W-1:0] lk_asid,
  input  logic [47:0]       lk_va,
  output logic              hit,
  output logic [1:0]        hit_level,
  output logic [47:0]       hit_pa,
  output logic              hit_ap1,
  output logic              hit_ap2,
  output logic              hit_pxn,
  output logic              hit_uxn,

  // Fill
  input  logic              fill,
  input  logic [ASID_W-1:0] fill_asid,
  input  logic [1:0]        fill_level,
  input  logic [47:12]      fill_va,
  input  logic [47:12]      fill_pa,
  input  logic              fill_ap1,
  input  logic              fill_ap2,
  input  logic              fill_pxn,
  input  logic              fill_uxn
);

  localparam int unsigned IDX_W = (ENTRIES > 1) ? $clog2(ENTRIES) : 1;

  logic [ENTRIES-1:0]             v_q;
  logic [ENTRIES-1:0][ASID_W-1:0] asid_q;
  logic [ENTRIES-1:0][1:0]        lvl_q;
  logic [ENTRIES-1:0][35:0]       vtag_q;     // VA[47:12]
  logic [ENTRIES-1:0][35:0]       ptag_q;     // PA[47:12]
  logic [ENTRIES-1:0][3:0]        perm_q;     // {ap1, ap2, pxn, uxn}
  logic [IDX_W-1:0]               rr_q;

  // Which of VA[47:12] take part in the compare at each level
  function automatic [35:0] tag_mask(input [1:0] lvl);
    case (lvl)
      2'd1:    tag_mask = 36'hFFFFC0000;    // 1 GB: VA[47:30]
      2'd2:    tag_mask = 36'hFFFFFFE00;    // 2 MB: VA[47:21]
      default: tag_mask = 36'hFFFFFFFFF;    // 4 KB: VA[47:12]
    endcase
  endfunction

  // ---------------- lookup ----------------
  logic [ENTRIES-1:0] hit_vec;
  logic [IDX_W-1:0]   hit_idx;

  always_comb begin
    for (int i = 0; i < ENTRIES; i++) begin
      hit_vec[i] = v_q[i] && (asid_q[i] == lk_asid) &&
                   (((vtag_q[i] ^ lk_va[47:12]) & tag_mask(lvl_q[i])) == 36'b0);
    end
  end

  always_comb begin
    logic found;
    found   = 1'b0;
    hit_idx = '0;
    for (int i = 0; i < ENTRIES; i++) begin
      if (hit_vec[i] && !found) begin
        found   = 1'b1;
        hit_idx = IDX_W'(i);
      end
    end
  end

  logic [35:0] hmask;
  assign hit       = |hit_vec;
  assign hit_level = lvl_q[hit_idx];
  assign hmask     = tag_mask(lvl_q[hit_idx]);
  assign hit_pa    = {(ptag_q[hit_idx] & hmask) | (lk_va[47:12] & ~hmask), lk_va[11:0]};
  assign {hit_ap1, hit_ap2, hit_pxn, hit_uxn} = perm_q[hit_idx];

  // ---------------- victim selection ----------------
  logic [35:0]      fmask;
  logic [IDX_W-1:0] victim;
  logic             victim_is_rr;

  assign fmask = tag_mask(fill_level);

  always_comb begin
    logic found_dup, found_free;
    logic [IDX_W-1:0] dup_idx, free_idx;
    found_dup  = 1'b0;
    found_free = 1'b0;
    dup_idx    = '0;
    free_idx   = '0;
    for (int i = 0; i < ENTRIES; i++) begin
      if (!found_dup && v_q[i] && asid_q[i] == fill_asid && lvl_q[i] == fill_level &&
          ((vtag_q[i] ^ fill_va) & fmask) == 36'b0) begin
        found_dup = 1'b1;
        dup_idx   = IDX_W'(i);
      end
      if (!found_free && !v_q[i]) begin
        found_free = 1'b1;
        free_idx   = IDX_W'(i);
      end
    end
    victim_is_rr = !found_dup && !found_free;
    victim       = found_dup ? dup_idx : found_free ? free_idx : rr_q;
  end

  // ---------------- selective invalidation ----------------
  logic [ENTRIES-1:0] inv_vec;

  always_comb begin
    for (int i = 0; i < ENTRIES; i++) begin
      inv_vec[i] = (!inv_asid_en || asid_q[i] == inv_asid) &&
                   (!inv_va_en   || ((vtag_q[i] ^ inv_va) & tag_mask(lvl_q[i])) == 36'b0);
    end
  end

  // ---------------- state ----------------
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      v_q    <= '0;
      asid_q <= '0;
      lvl_q  <= '0;
      vtag_q <= '0;
      ptag_q <= '0;
      perm_q <= '0;
      rr_q   <= '0;
    end else if (inv_all) begin
      v_q <= '0;
    end else if (inv_sel) begin
      v_q <= v_q & ~inv_vec;
    end else if (fill) begin
      v_q[victim]    <= 1'b1;
      asid_q[victim] <= fill_asid;
      lvl_q[victim]  <= fill_level;
      vtag_q[victim] <= fill_va;
      ptag_q[victim] <= fill_pa;
      perm_q[victim] <= {fill_ap1, fill_ap2, fill_pxn, fill_uxn};
      if (victim_is_rr) begin
        rr_q <= (rr_q == IDX_W'(ENTRIES - 1)) ? '0 : rr_q + 1'b1;
      end
    end
  end

`ifdef SG_ASSERT
  // With correct software (tables changed only together with an
  // invalidation, distinct ASIDs per address space) no address can
  // match two entries.
  a_tlb_single_hit: assert property (@(posedge clk) disable iff (!rst_n)
    $onehot0(hit_vec))
    else $error("TLB: address matches more than one entry");
`endif

endmodule
