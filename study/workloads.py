"""Synthetic device traffic for the TLB sizing study.

Each workload is what one or more devices would send through StreamGuard:
a set of mappings per stream (its page tables) and an ordered list of
accesses. One access is one AXI transaction, so one translation; its
length does not matter to the caches.

The workloads are built from simple, documented rules, not recorded from
real hardware. They are meant to have the locality patterns of real
devices, and every number that shapes them is in this file.

    dma_copy      one DMA engine copying a buffer: two pages in use at once
    npu_conv      an NPU running one 3x3 convolution layer, row by row:
                  about 15 pages in use at once, weights re-read every row
    packet_rx     a network device writing packets into scattered buffers:
                  almost every packet lands on a new page
    multi_device  four devices sharing the unit, taking turns every few
                  transactions
    npu_conv_2m   npu_conv with every buffer mapped as 2 MB blocks
"""

import random
from dataclasses import dataclass, field

PAGE, BLOCK_2M = 3, 2                 # leaf level of a mapping
BURST = 256                           # bytes per transaction for streaming devices
DATA_HI = 1 << 47                     # physical addresses live in the upper half


@dataclass
class Workload:
    name: str
    description: str
    # sid -> list of (va, pa, level): the leaf mappings of that stream
    mappings: dict = field(default_factory=dict)
    # (sid, va, write) in issue order
    accesses: list = field(default_factory=list)

    def asid(self, sid):
        return sid                     # every stream has its own address space

    def map_buffer(self, sid, va, size, level=PAGE):
        """Map [va, va + size) with pages or 2 MB blocks."""
        step = 1 << (12 if level == PAGE else 21)
        start, end = va & ~(step - 1), va + size
        out = self.mappings.setdefault(sid, [])
        for a in range(start, end, step):
            out.append((a, DATA_HI | (sid << 40) | a, level))

    def stream(self, sid, va, size, write, burst=BURST):
        """Sequential transfer of `size` bytes in bursts."""
        for off in range(0, size, burst):
            self.accesses.append((sid, va + off, write))

    @property
    def pages_touched(self):
        return len({(s, va >> 12) for s, va, _ in self.accesses})


# ---------------------------------------------------------------------
def dma_copy():
    w = Workload("dma_copy", "DMA engine copying 512 KB: read a burst, write a burst")
    src, dst, size = 0x1000_0000, 0x2000_0000, 512 << 10
    w.map_buffer(1, src, size)
    w.map_buffer(1, dst, size)
    for off in range(0, size, BURST):
        w.accesses.append((1, src + off, False))
        w.accesses.append((1, dst + off, True))
    return w


# NHWC int8 tensors of a ResNet-style 3x3 convolution, 64 -> 64 channels
CONV_H = CONV_W = 56
CONV_C = 64
ROW_BYTES = CONV_W * CONV_C                       # 3584 bytes per image row
FMAP_BYTES = CONV_H * ROW_BYTES                   # 200,704 bytes, about 49 pages
WEIGHT_BYTES = 3 * 3 * CONV_C * CONV_C            # 36,864 bytes, 9 pages


def _npu_conv(name, level, description):
    w = Workload(name, description)
    if level == PAGE:
        ifm, wts, ofm = 0x4000_0000, 0x4010_0000, 0x4020_0000
    else:
        # each buffer in its own 2 MB block
        ifm, wts, ofm = 0x4000_0000, 0x4040_0000, 0x4080_0000
    w.map_buffer(2, ifm, FMAP_BYTES, level)
    w.map_buffer(2, wts, WEIGHT_BYTES, level)
    w.map_buffer(2, ofm, FMAP_BYTES, level)
    # One output row at a time: the three input rows it needs, then all
    # the weights (no room to keep them on chip), then the output row.
    for y in range(CONV_H):
        for r in (y - 1, y, y + 1):
            if 0 <= r < CONV_H:
                w.stream(2, ifm + r * ROW_BYTES, ROW_BYTES, False)
        w.stream(2, wts, WEIGHT_BYTES, False)
        w.stream(2, ofm + y * ROW_BYTES, ROW_BYTES, True)
    return w


def npu_conv():
    return _npu_conv("npu_conv", PAGE,
                     "NPU, 3x3 convolution 56x56x64 -> 56x56x64, one output row at a time, 4 KB pages")


def npu_conv_2m():
    return _npu_conv("npu_conv_2m", BLOCK_2M,
                     "the same NPU layer with every buffer mapped as 2 MB blocks")


def packet_rx(packets=1500, seed=1):
    """A network device receiving packets. For each packet: read the next
    descriptor, write the packet into the buffer it points to, write the
    descriptor back. Buffers come from a 32 MB pool in random order, as
    after a while of allocating and freeing."""
    rng = random.Random(seed)
    w = Workload("packet_rx", "network device writing 1500 packets into buffers spread over 32 MB")
    ring, ring_bytes = 0x6000_0000, 16 << 10          # 1024 descriptors of 16 bytes
    pool, pool_pages = 0x7000_0000, (32 << 20) >> 12
    w.map_buffer(3, ring, ring_bytes)
    used = set()
    for i in range(packets):
        desc = ring + (i % (ring_bytes // 16)) * 16
        page = rng.randrange(pool_pages)
        used.add(page)
        length = rng.choice([64, 64, 128, 256, 512, 1024, 1500, 1500])   # bimodal, like real traffic
        w.accesses.append((3, desc, False))
        w.stream(3, pool + page * 4096, length, True)
        w.accesses.append((3, desc, True))
    out = w.mappings.setdefault(3, [])
    for page in sorted(used):
        va = pool + page * 4096
        out.append((va, DATA_HI | (3 << 40) | va, PAGE))
    return w


def multi_device(rounds=3000, seed=2):
    """Four devices taking turns, a few transactions each:

        sid 4  camera writing frames into a 1 MB buffer, sequentially
        sid 5  small accelerator re-reading 24 KB of coefficients
        sid 6  audio device cycling through an 8 KB ring
        sid 7  USB-like device reading 512-byte blocks from a 4 MB area
    """
    rng = random.Random(seed)
    w = Workload("multi_device", "four devices sharing the unit, switching every 1 to 4 transactions")
    cam, cam_size = 0x8000_0000, 1 << 20
    acc, acc_size = 0x8100_0000, 24 << 10
    aud, aud_size = 0x8200_0000, 8 << 10
    usb, usb_size = 0x8300_0000, 4 << 20
    w.map_buffer(4, cam, cam_size)
    w.map_buffer(5, acc, acc_size)
    w.map_buffer(6, aud, aud_size)
    w.map_buffer(7, usb, usb_size)
    pos = {4: 0, 5: 0, 6: 0, 7: 0}
    usb_block = 0
    for _ in range(rounds // 4):
        for sid in (4, 5, 6, 7):
            for _ in range(rng.randint(1, 4)):
                if sid == 4:
                    w.accesses.append((4, cam + pos[4], True))
                    pos[4] = (pos[4] + BURST) % cam_size
                elif sid == 5:
                    w.accesses.append((5, acc + pos[5], False))
                    pos[5] = (pos[5] + BURST) % acc_size
                elif sid == 6:
                    w.accesses.append((6, aud + pos[6], rng.random() < 0.5))
                    pos[6] = (pos[6] + 64) % aud_size
                else:
                    if pos[7] % 512 == 0:
                        usb_block = rng.randrange(usb_size // 512)
                    w.accesses.append((7, usb + usb_block * 512 + pos[7] % 512, False))
                    pos[7] += 64
    return w


ALL = [dma_copy, npu_conv, packet_rx, multi_device, npu_conv_2m]
MAIN = ["dma_copy", "npu_conv", "packet_rx", "multi_device"]     # averaged in the study


def build_all():
    return {f().name: f() for f in ALL}


if __name__ == "__main__":
    for name, w in build_all().items():
        n_map = sum(len(m) for m in w.mappings.values())
        print(f"{name:13s} {len(w.accesses):6d} accesses, {w.pages_touched:5d} pages touched, "
              f"{n_map:5d} mappings, streams {sorted(w.mappings)}")
