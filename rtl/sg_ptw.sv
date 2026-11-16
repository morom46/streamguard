// StreamGuard - page table walker.
//
// Walks an AArch64 stage 1 translation table (VMSAv8-64, 4 KB granule,
// TTB0 region) for one input address and returns either the output
// address or a fault.
//
//   level   index bits    one entry maps
//     0     VA[47:39]     512 GB  (table only)
//     1     VA[38:30]     1 GB    (table or block)
//     2     VA[29:21]     2 MB    (table or block)
//     3     VA[20:12]     4 KB    (page)
//
// T0SZ sets the size of the input address space (2^(64-T0SZ)) and
// with it the level the walk starts at:
//
//   T0SZ 16..24 -> level 0,  25..33 -> level 1,  34..39 -> level 2
//
// Checks, in priority order:
//   translation fault   input address out of range, invalid descriptor,
//                       or an encoding that is reserved at that level
//   address size fault  descriptor address bits [51:48] set (OAS is 48)
//   access flag fault   leaf has AF = 0 (unless the CD sets AFFD)
//   permission fault    write to read-only, unprivileged access without
//                       AP[1], or instruction fetch from an XN mapping
//
// Hierarchical attributes in table descriptors (APTable, XNTable) are
// not applied.
//
// Walk cache support: with start_l3 set the walk begins at level 3 in
// l3_table, skipping the three upper reads. The walker reports the
// level 3 table it passed through (l3tab) so it can be cached, and the
// leaf descriptor it ended on (leaf_desc) so the TLB can be filled.

module sg_ptw #(
  parameter int unsigned ADDR_W = 48
) (
  input  logic              clk,
  input  logic              rst_n,

  // Walk request: start is a one cycle pulse, inputs are held by the
  // caller until done
  input  logic              start,
  input  logic [ADDR_W-1:0] va,
  input  logic [47:12]      ttb,          // level-0..2 start table
  input  logic [5:0]        t0sz,         // caller guarantees 16..39
  input  logic              affd,
  input  logic              is_write,
  input  logic [2:0]        prot,         // AxPROT of the access
  input  logic              start_l3,     // walk cache hit: start at level 3
  input  logic [47:12]      l3_table,

  // Result, valid in the cycle done is high
  output logic              done,
  output logic [7:0]        fcode,        // sg_pkg::FC_NONE when successful
  output logic [1:0]        flevel,
  output logic [ADDR_W-1:0] pa,
  output logic [63:0]       leaf_desc,    // last descriptor read
  output logic              l3tab_valid,  // walk passed a level 2 table entry
  output logic [47:12]      l3tab,        // ... pointing at this level 3 table

  // Table memory read
  output logic              mem_req,
  output logic [ADDR_W-1:0] mem_addr,
  input  logic              mem_done,
  input  logic [63:0]       mem_data,
  input  logic              mem_err
);

  typedef enum logic [2:0] {
    W_IDLE, W_READ, W_WAIT, W_DECODE, W_DONE
  } state_e;

  state_e       state_q;
  logic [1:0]   level_q;
  logic [47:12] table_q;
  logic [63:0]  desc_q;
  logic         desc_err_q;

  // ---------------- start level and input range ----------------
  logic [1:0] start_level;
  logic       va_out_of_range;

  always_comb begin
    if      (t0sz <= 6'd24) start_level = 2'd0;
    else if (t0sz <= 6'd33) start_level = 2'd1;
    else                    start_level = 2'd2;
  end

  // The input space is 2^(64-T0SZ) bytes. Any address bit at or
  // above that size must be zero.
  logic [6:0]        in_bits;
  logic [ADDR_W-1:0] in_mask;
  assign in_bits         = 7'd64 - {1'b0, t0sz};
  assign in_mask         = ({{(ADDR_W-1){1'b0}}, 1'b1} << in_bits) - 1'b1;
  assign va_out_of_range = |(va & ~in_mask);

  // ---------------- descriptor address ----------------
  logic [8:0] index;
  always_comb begin
    case (level_q)
      2'd0:    index = va[47:39];
      2'd1:    index = va[38:30];
      2'd2:    index = va[29:21];
      default: index = va[20:12];
    endcase
  end

  assign mem_req  = (state_q == W_READ);
  assign mem_addr = {table_q, index, 3'b000};

  // ---------------- descriptor decode ----------------
  logic desc_valid, desc_is_table, desc_is_leaf, desc_reserved;
  logic addr_size_bad, af_bad, perm_bad;
  assign desc_valid    = desc_q[sg_pkg::DESC_VALID];
  // bit 1 set: next-level table at levels 0-2, page at level 3
  assign desc_is_table = desc_valid &  desc_q[sg_pkg::DESC_TABLE] & (level_q != 2'd3);
  // leaf: page at level 3, block at levels 1 and 2
  assign desc_is_leaf  = desc_valid &
                         (( desc_q[sg_pkg::DESC_TABLE] & (level_q == 2'd3)) |
                          (~desc_q[sg_pkg::DESC_TABLE] & (level_q == 2'd1 || level_q == 2'd2)));
  // 0b01 at level 0 (no 512 GB blocks) or level 3 is reserved
  assign desc_reserved = desc_valid & ~desc_is_table & ~desc_is_leaf;

  assign addr_size_bad = (desc_q[51:48] != 4'b0);
  assign af_bad        = ~desc_q[sg_pkg::DESC_AF] & ~affd;

  sg_perm u_perm (
    .is_write (is_write),
    .prot     (prot),
    .ap1      (desc_q[sg_pkg::DESC_AP1]),
    .ap2      (desc_q[sg_pkg::DESC_AP2]),
    .pxn      (desc_q[sg_pkg::DESC_PXN]),
    .uxn      (desc_q[sg_pkg::DESC_UXN]),
    .denied   (perm_bad)
  );

  // Output address: descriptor supplies the upper bits, the input
  // address supplies the offset inside the page or block.
  logic [ADDR_W-1:0] leaf_pa;
  always_comb begin
    case (level_q)
      2'd1:    leaf_pa = {desc_q[47:30], va[29:0]};   // 1 GB block
      2'd2:    leaf_pa = {desc_q[47:21], va[20:0]};   // 2 MB block
      default: leaf_pa = {desc_q[47:12], va[11:0]};   // 4 KB page
    endcase
  end

  // ---------------- control ----------------
  assign done      = (state_q == W_DONE);
  assign leaf_desc = desc_q;

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state_q    <= W_IDLE;
      level_q    <= 2'd0;
      table_q    <= '0;
      desc_q     <= '0;
      desc_err_q <= 1'b0;
      fcode      <= sg_pkg::FC_NONE;
      flevel     <= 2'd0;
      pa         <= '0;
      l3tab_valid <= 1'b0;
      l3tab      <= '0;
    end else begin
      case (state_q)
        W_IDLE: begin
          if (start) begin
            level_q     <= start_l3 ? 2'd3 : start_level;
            table_q     <= start_l3 ? l3_table : ttb;
            flevel      <= start_level;
            l3tab_valid <= 1'b0;
            if (va_out_of_range) begin
              fcode   <= sg_pkg::FC_TRANSLATION;
              state_q <= W_DONE;
            end else begin
              state_q <= W_READ;
            end
          end
        end

        // mem_req is high for this one cycle; the read port has
        // latched nothing itself, so mem_addr stays stable in W_WAIT
        W_READ: state_q <= W_WAIT;

        W_WAIT: begin
          if (mem_done) begin
            desc_q     <= mem_data;
            desc_err_q <= mem_err;
            state_q    <= W_DECODE;
          end
        end

        W_DECODE: begin
          flevel  <= level_q;
          state_q <= W_DONE;
          if (desc_err_q) begin
            fcode <= sg_pkg::FC_WALK_EABT;
          end else if (!desc_valid || desc_reserved) begin
            fcode <= sg_pkg::FC_TRANSLATION;
          end else if (addr_size_bad) begin
            fcode <= sg_pkg::FC_ADDR_SIZE;
          end else if (desc_is_table) begin
            if (level_q == 2'd2) begin
              l3tab_valid <= 1'b1;
              l3tab       <= desc_q[47:12];
            end
            table_q <= desc_q[47:12];
            level_q <= level_q + 2'd1;
            state_q <= W_READ;
          end else if (af_bad) begin
            fcode <= sg_pkg::FC_ACCESS;
          end else if (perm_bad) begin
            fcode <= sg_pkg::FC_PERMISSION;
          end else begin
            fcode <= sg_pkg::FC_NONE;
            pa    <= leaf_pa;
          end
        end

        W_DONE: state_q <= W_IDLE;

        default: state_q <= W_IDLE;
      endcase
    end
  end

endmodule
