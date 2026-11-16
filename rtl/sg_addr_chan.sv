// StreamGuard - address channel handler.
//
// One instance sits on AW and one on AR. It takes a single address
// request from the upstream device, asks for a translation decision,
// and then either forwards the request downstream with the output
// address or hands it to the terminate logic.
//
// One request is in flight at a time. That keeps the control simple
// and correct first; a fast path for TLB hits is a later optimisation.

module sg_addr_chan #(
  parameter int unsigned ADDR_W = 48,
  parameter int unsigned SID_W  = 8,
  parameter int unsigned PAY_W  = 1    // everything except the address
) (
  input  logic              clk,
  input  logic              rst_n,

  // New requests are only accepted while this is high
  input  logic              allow_i,

  // Upstream (device side)
  input  logic              s_valid,
  output logic              s_ready,
  input  logic [ADDR_W-1:0] s_addr,
  input  logic [SID_W-1:0]  s_sid,
  input  logic [PAY_W-1:0]  s_pay,

  // Translation request / response
  output logic              x_req_valid,
  input  logic              x_req_ready,
  output logic [ADDR_W-1:0] x_req_addr,
  output logic [SID_W-1:0]  x_req_sid,
  input  logic              x_rsp_valid,
  input  logic              x_rsp_fault,
  input  logic [ADDR_W-1:0] x_rsp_addr,

  // Decision strobe, one cycle per request
  output logic              dec_valid_o,
  output logic              dec_fault_o,

  // Downstream (memory side)
  output logic              m_valid,
  input  logic              m_ready,
  output logic [ADDR_W-1:0] m_addr,
  output logic [PAY_W-1:0]  m_pay,

  // Hand-off of a terminated request
  output logic              term_valid,
  input  logic              term_ready,
  output logic [PAY_W-1:0]  term_pay
);

  typedef enum logic [2:0] {
    S_IDLE,    // waiting for a request
    S_XREQ,    // translation request pending
    S_XWAIT,   // waiting for the decision
    S_FWD,     // presenting the request downstream
    S_TERM     // handing the request to the terminate logic
  } state_e;

  state_e            state_q;
  logic [ADDR_W-1:0] addr_q;
  logic [SID_W-1:0]  sid_q;
  logic [PAY_W-1:0]  pay_q;

  assign s_ready     = (state_q == S_IDLE) & allow_i;

  assign x_req_valid = (state_q == S_XREQ);
  assign x_req_addr  = addr_q;
  assign x_req_sid   = sid_q;

  assign dec_valid_o = (state_q == S_XWAIT) & x_rsp_valid;
  assign dec_fault_o = x_rsp_fault;

  assign m_valid     = (state_q == S_FWD);
  assign m_addr      = addr_q;
  assign m_pay       = pay_q;

  assign term_valid  = (state_q == S_TERM);
  assign term_pay    = pay_q;

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state_q <= S_IDLE;
      addr_q  <= '0;
      sid_q   <= '0;
      pay_q   <= '0;
    end else begin
      case (state_q)
        S_IDLE: begin
          if (s_valid && s_ready) begin
            addr_q  <= s_addr;
            sid_q   <= s_sid;
            pay_q   <= s_pay;
            state_q <= S_XREQ;
          end
        end

        S_XREQ: begin
          if (x_req_ready) state_q <= S_XWAIT;
        end

        S_XWAIT: begin
          if (x_rsp_valid) begin
            if (x_rsp_fault) begin
              state_q <= S_TERM;
            end else begin
              addr_q  <= x_rsp_addr;
              state_q <= S_FWD;
            end
          end
        end

        S_FWD: begin
          if (m_ready) state_q <= S_IDLE;
        end

        S_TERM: begin
          if (term_ready) state_q <= S_IDLE;
        end

        default: state_q <= S_IDLE;
      endcase
    end
  end

endmodule
