// StreamGuard - small exact-match cache.
//
// Fully associative, combinational lookup on an exact tag. Used for
// the configuration cache (tag = StreamID) and the walk cache
// (tag = ASID and VA[47:21]).
//
// Fill and invalidate follow the same rules as the TLB: overwrite an
// entry with the same tag, else the first empty entry, else the round
// robin victim; invalidate wins over a fill in the same cycle.
//
// inv_all clears every entry. inv_sel clears the entries whose tag
// matches inv_tag in the bit positions set in inv_mask (a mask of all
// zeros selects every entry).

module sg_tagcache #(
  parameter int unsigned ENTRIES = 4,
  parameter int unsigned TAG_W   = 8,
  parameter int unsigned DATA_W  = 8
) (
  input  logic              clk,
  input  logic              rst_n,

  input  logic              inv_all,
  input  logic              inv_sel,
  input  logic [TAG_W-1:0]  inv_tag,
  input  logic [TAG_W-1:0]  inv_mask,

  input  logic [TAG_W-1:0]  lk_tag,
  output logic              hit,
  output logic [DATA_W-1:0] hit_data,

  input  logic              fill,
  input  logic [TAG_W-1:0]  fill_tag,
  input  logic [DATA_W-1:0] fill_data
);

  localparam int unsigned IDX_W = (ENTRIES > 1) ? $clog2(ENTRIES) : 1;

  logic [ENTRIES-1:0]             v_q;
  logic [ENTRIES-1:0][TAG_W-1:0]  tag_q;
  logic [ENTRIES-1:0][DATA_W-1:0] data_q;
  logic [IDX_W-1:0]               rr_q;

  // ---------------- lookup ----------------
  logic [ENTRIES-1:0] hit_vec;
  logic [IDX_W-1:0]   hit_idx;

  always_comb begin
    logic found;
    found   = 1'b0;
    hit_idx = '0;
    for (int i = 0; i < ENTRIES; i++) begin
      hit_vec[i] = v_q[i] && (tag_q[i] == lk_tag);
      if (hit_vec[i] && !found) begin
        found   = 1'b1;
        hit_idx = IDX_W'(i);
      end
    end
  end

  assign hit      = |hit_vec;
  assign hit_data = data_q[hit_idx];

  // ---------------- victim selection ----------------
  logic [IDX_W-1:0] victim;
  logic             victim_is_rr;

  always_comb begin
    logic found_dup, found_free;
    logic [IDX_W-1:0] dup_idx, free_idx;
    found_dup  = 1'b0;
    found_free = 1'b0;
    dup_idx    = '0;
    free_idx   = '0;
    for (int i = 0; i < ENTRIES; i++) begin
      if (!found_dup && v_q[i] && tag_q[i] == fill_tag) begin
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
      inv_vec[i] = (((tag_q[i] ^ inv_tag) & inv_mask) == '0);
    end
  end

  // ---------------- state ----------------
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      v_q    <= '0;
      tag_q  <= '0;
      data_q <= '0;
      rr_q   <= '0;
    end else if (inv_all) begin
      v_q <= '0;
    end else if (inv_sel) begin
      v_q <= v_q & ~inv_vec;
    end else if (fill) begin
      v_q[victim]    <= 1'b1;
      tag_q[victim]  <= fill_tag;
      data_q[victim] <= fill_data;
      if (victim_is_rr) begin
        rr_q <= (rr_q == IDX_W'(ENTRIES - 1)) ? '0 : rr_q + 1'b1;
      end
    end
  end

`ifdef SG_ASSERT
  a_cache_single_hit: assert property (@(posedge clk) disable iff (!rst_n)
    $onehot0(hit_vec))
    else $error("tag cache: tag matches more than one entry");
`endif

endmodule
