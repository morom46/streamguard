// StreamGuard - AXI handshake checker.
//
// AMBA AXI rule: once VALID is asserted it must stay asserted, with
// the payload unchanged, until READY is seen. Instantiated on every
// channel StreamGuard drives when SG_ASSERT is defined.

module sg_valid_stable #(
  parameter int unsigned W = 1
) (
  input logic         clk,
  input logic         rst_n,
  input logic         valid,
  input logic         ready,
  input logic [W-1:0] payload
);

  property p_valid_held;
    @(posedge clk) disable iff (!rst_n)
      (valid && !ready) |=> valid;
  endproperty

  property p_payload_stable;
    @(posedge clk) disable iff (!rst_n)
      (valid && !ready) |=> $stable(payload);
  endproperty

  a_valid_held:     assert property (p_valid_held)
    else $error("VALID dropped before READY");
  a_payload_stable: assert property (p_payload_stable)
    else $error("payload changed while VALID was waiting for READY");

endmodule
