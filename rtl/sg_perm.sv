// StreamGuard - permission check.
//
// Decides whether an access may use a leaf translation, from the
// direction of the access, its AxPROT and the leaf's AP[2:1], PXN and
// UXN bits. Used by the walker and again on every TLB hit, so both
// paths apply exactly the same rule.
//
//   write to a read-only mapping (AP[2] = 1)          -> denied
//   unprivileged access, AP[1] = 0 (no EL0 access)    -> denied
//   instruction read, privileged,   PXN = 1           -> denied
//   instruction read, unprivileged, UXN = 1           -> denied

module sg_perm (
  input  logic       is_write,
  input  logic [2:0] prot,        // AxPROT: [0] privileged, [2] instruction
  input  logic       ap1,
  input  logic       ap2,
  input  logic       pxn,
  input  logic       uxn,
  output logic       denied
);

  logic privileged, instruction;
  assign privileged  = prot[0];
  assign instruction = prot[2];

  always_comb begin
    denied = 1'b0;
    if (is_write && ap2)            denied = 1'b1;
    if (!privileged && !ap1)        denied = 1'b1;
    if (instruction && !is_write) begin
      if ( privileged && pxn)       denied = 1'b1;
      if (!privileged && uxn)       denied = 1'b1;
    end
  end

`ifdef FORMAL
  // The complete rule, written as a table of cases and proved for
  // every input combination. Run with: make formal
  always_comb begin
    if (is_write) begin
      // a write never looks at the execute-never bits or at "instruction"
      if (privileged) assert (denied == ap2);
      else            assert (denied == (ap2 | ~ap1));
    end else if (!instruction) begin
      // data read
      if (privileged) assert (!denied);
      else            assert (denied == ~ap1);
    end else begin
      // instruction read
      if (privileged) assert (denied == pxn);
      else            assert (denied == (uxn | ~ap1));
    end
  end
`endif

endmodule
