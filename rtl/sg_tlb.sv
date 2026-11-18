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
// entry, or a victim chosen by the replacement policy:
//
//   POLICY = 0  round robin: entries are replaced in turn, whether
//               they are being used or not
//   POLICY = 1  not recently used (NRU): one "used" bit per entry, set
//               when the entry is filled or when a hit on it is used
//               (touch). When the last bit would be set, all the
//               others are cleared instead. The victim is the lowest
//               numbered entry whose bit is clear, so an entry that
//               was hit since the last clear is never replaced.
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
  parameter int unsigned ASID_W  = 8,
  parameter int unsigned POLICY  = 0      // 0: round robin, 1: NRU
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
  input  logic              touch,        // the hit above was used (NRU only)

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
  logic [ENTRIES-1:0]             used_q;     // NRU only

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
  logic             victim_is_rr;      // chosen by the policy
  logic [IDX_W-1:0] nru_idx;           // lowest entry not recently used

  always_comb begin
    logic found;
    found   = 1'b0;
    nru_idx = '0;
    for (int i = 0; i < ENTRIES; i++) begin
      if (!found && !used_q[i]) begin
        found   = 1'b1;
        nru_idx = IDX_W'(i);
      end
    end
  end

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
    victim       = found_dup  ? dup_idx  :
                   found_free ? free_idx :
                   (POLICY == 1) ? nru_idx : rr_q;
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

  // ---------------- NRU used bits ----------------
  // Only built when the NRU policy is selected; with round robin the
  // bits are constant zero and take no area.
  if (POLICY == 1) begin : g_nru
    logic [ENTRIES-1:0] mark, used_set;

    always_comb begin
      mark = '0;
      if (touch && hit) mark[hit_idx] = 1'b1;
      if (fill)         mark[victim]  = 1'b1;
      used_set = used_q | mark;
      // never let every bit be set: keep only the newest marks
      if (&used_set) used_set = mark;
    end

    always_ff @(posedge clk or negedge rst_n) begin
      if (!rst_n)       used_q <= '0;
      else if (inv_all) used_q <= '0;
      else if (inv_sel) used_q <= used_q & ~inv_vec;
      else              used_q <= used_set;
    end
  end else begin : g_no_nru
    assign used_q = '0;
    logic unused_touch;
    assign unused_touch = touch;
  end

`ifdef SG_ASSERT
  // With correct software (tables changed only together with an
  // invalidation, distinct ASIDs per address space) no address can
  // match two entries.
  a_tlb_single_hit: assert property (@(posedge clk) disable iff (!rst_n)
    $onehot0(hit_vec))
    else $error("TLB: address matches more than one entry");
`endif

`ifdef FORMAL
  // Proved for every sequence of fills, invalidations and lookups
  // (make formal). The proof watches one arbitrary page of one
  // arbitrary address space and tracks whether a translation for it
  // may be cached.
  (* anyconst *) logic [ASID_W-1:0] f_asid;
  (* anyconst *) logic [47:12]      f_va;
  logic f_may;
  logic f_past_valid;

  initial f_past_valid = 1'b0;
  always @(posedge clk) f_past_valid <= 1'b1;
  initial assume (!rst_n);

  // "Same page or block", written out separately from tag_mask so that
  // a wrong mask in the design is seen by the proof.
  function automatic f_same(input [47:12] a, input [47:12] b, input [1:0] lvl);
    case (lvl)
      2'd1:    f_same = (a[47:30] == b[47:30]);     // 1 GB block
      2'd2:    f_same = (a[47:21] == b[47:21]);     // 2 MB block
      default: f_same = (a == b);                   // 4 KB page
    endcase
  endfunction

  // A fill covers the watched page; an invalidation names it
  logic f_fill_covers, f_inv_names;
  assign f_fill_covers = (fill_asid == f_asid) && f_same(fill_va, f_va, fill_level);
  assign f_inv_names   = (!inv_asid_en || inv_asid == f_asid) &&
                         (!inv_va_en   || inv_va == f_va);

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n)                     f_may <= 1'b0;
    else if (inv_all)               f_may <= 1'b0;
    else if (inv_sel) begin
      if (f_inv_names)              f_may <= 1'b0;
    end
    else if (fill && f_fill_covers) f_may <= 1'b1;
  end

  always @(posedge clk) begin
    if (rst_n) begin
      // 1. An entry covering the watched page exists only if one was
      //    filled and the page was not invalidated since.
      for (int i = 0; i < ENTRIES; i++) begin
        if (v_q[i] && asid_q[i] == f_asid &&
            ((vtag_q[i] ^ f_va) & tag_mask(lvl_q[i])) == 36'b0) begin
          assert (f_may);
        end
      end
      // What the lookup sees: an invalidated page never hits until it
      // is filled again, whatever size of entry covered it.
      if (lk_asid == f_asid && lk_va[47:12] == f_va && hit) assert (f_may);

      // 2. The output address keeps the offset inside the page or block.
      if (hit) begin
        assert (hit_pa[11:0] == lk_va[11:0]);
        if (hit_level == 2'd2) assert (hit_pa[20:0] == lk_va[20:0]);
        if (hit_level == 2'd1) assert (hit_pa[29:0] == lk_va[29:0]);
      end
    end
    // 4. NRU: there is always an entry not recently used, and the
    //    policy never picks one that is marked used.
    if (rst_n && POLICY == 1 && ENTRIES > 1) begin
      assert (!(&used_q));
      if (fill && victim_is_rr) assert (!used_q[victim]);
    end

    // 3. A fill is visible to the next lookup of that address.
    if (f_past_valid && rst_n && $past(rst_n) &&
        $past(fill && !inv_all && !inv_sel) &&
        lk_asid == $past(fill_asid) && lk_va[47:12] == $past(fill_va)) begin
      assert (hit);
    end
  end
`endif

endmodule
