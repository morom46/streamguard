// StreamGuard - shared constants.
//
// Register offsets and field positions follow the SMMUv3 register map
// (Arm IHI 0070, chapter 6). Only the subset StreamGuard implements is
// listed here; everything else reads as zero and ignores writes.

package sg_pkg;

  // ---------------------------------------------------------------
  // Register offsets, page 0. PADDR is 17 bits so that page 1
  // (EVENTQ_PROD/CONS at 0x100A8/0x100AC) is reachable later.
  // ---------------------------------------------------------------
  localparam logic [16:0] REG_IDR0   = 17'h00000;
  localparam logic [16:0] REG_IDR1   = 17'h00004;
  localparam logic [16:0] REG_IDR5   = 17'h00014;
  localparam logic [16:0] REG_CR0    = 17'h00020;
  localparam logic [16:0] REG_CR0ACK = 17'h00024;
  localparam logic [16:0] REG_GBPA   = 17'h00044;

  // CR0 / CR0ACK bit positions
  localparam int unsigned CR0_SMMUEN  = 0;
  localparam int unsigned CR0_EVENTQEN = 2;
  localparam int unsigned CR0_CMDQEN  = 3;
  localparam logic [31:0] CR0_WMASK   = 32'h0000_000D;

  // GBPA bit positions
  localparam int unsigned GBPA_ABORT  = 20;
  localparam int unsigned GBPA_UPDATE = 31;

  // ---------------------------------------------------------------
  // ID register values. These advertise what this implementation
  // supports so that a driver can probe it the way it would probe
  // any SMMUv3.
  // ---------------------------------------------------------------
  // IDR0: S1P=1 (stage 1 only), TTF=0b10 (AArch64 tables),
  //       TTENDIAN=0b10 (little endian), STALL_MODEL=0b01 (no stall),
  //       ST_LEVEL=0b00 (linear stream table), ASID16=0 (8-bit ASID)
  localparam logic [31:0] IDR0_VALUE = 32'h0140_000A;
  // IDR1: SIDSIZE=8, EVENTQS=4, CMDQS=4 (16-entry queues)
  localparam logic [31:0] IDR1_VALUE = 32'h0084_0008;
  // IDR5: OAS=0b101 (48-bit output address), GRAN4K=1
  localparam logic [31:0] IDR5_VALUE = 32'h0000_0015;

  // AXI response codes
  localparam logic [1:0] RESP_OKAY   = 2'b00;
  localparam logic [1:0] RESP_SLVERR = 2'b10;

endpackage
