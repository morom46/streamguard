// StreamGuard - translation decision.
//
// Every address request from the AW and AR channels comes through
// here and gets one of two answers: forward it with this output
// address, or terminate it.
//
// Milestone 1 implements the global decision only:
//
//   SMMUEN  GBPA.ABORT   result
//     0         0        bypass (output address = input address)
//     0         1        terminate
//     1         x        terminate (translation not implemented yet)
//
// Terminating while enabled is deliberate. Until the stream table,
// page walker and TLB exist, an enabled unit has no way to prove an
// access is allowed, so it fails closed.

module sg_xlate #(
  parameter int unsigned ADDR_W = 48,
  parameter int unsigned SID_W  = 8
) (
  input  logic              clk,
  input  logic              rst_n,

  input  logic              smmuen_i,
  input  logic              gbpa_abort_i,

  // Request
  input  logic              req_valid,
  output logic              req_ready,
  input  logic [ADDR_W-1:0] req_addr,
  input  logic [SID_W-1:0]  req_sid,
  input  logic              req_write,

  // Response, one per request, in order
  output logic              rsp_valid,
  output logic              rsp_fault,
  output logic [ADDR_W-1:0] rsp_addr
);

  assign req_ready = 1'b1;

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      rsp_valid <= 1'b0;
      rsp_fault <= 1'b0;
      rsp_addr  <= '0;
    end else begin
      rsp_valid <= req_valid;
      if (req_valid) begin
        rsp_fault <= smmuen_i | gbpa_abort_i;
        rsp_addr  <= req_addr;
      end
    end
  end

  // StreamID and direction are not used by the bypass decision.
  logic unused;
  assign unused = &{1'b0, req_sid, req_write};

endmodule
