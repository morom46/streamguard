"""Command and event queue formats.

Commands are 16 bytes: two 64-bit words, opcode in bits [7:0] of the
first. Each builder returns (word0, word1).

Event records are 32 bytes: four 64-bit words. Event.parse turns them
into the fields the tests compare.
"""

from dataclasses import dataclass

CMD_PREFETCH_CONFIG = 0x01
CMD_PREFETCH_ADDR = 0x02
CMD_CFGI_STE = 0x03
CMD_CFGI_STE_RANGE = 0x04
CMD_CFGI_CD = 0x05
CMD_CFGI_CD_ALL = 0x06
CMD_TLBI_NH_ALL = 0x10
CMD_TLBI_NH_ASID = 0x11
CMD_TLBI_NH_VA = 0x12
CMD_TLBI_NH_VAA = 0x13
CMD_TLBI_NSNH_ALL = 0x30
CMD_SYNC = 0x46

CERROR_NONE, CERROR_ILL, CERROR_ABT = 0, 1, 2

PAGE_MASK = ~0xFFF & ((1 << 64) - 1)


def sync():
    return (CMD_SYNC, 0)


def prefetch_config(sid):
    return (CMD_PREFETCH_CONFIG | (sid << 32), 0)


def cfgi_ste(sid, leaf=1):
    return (CMD_CFGI_STE | (sid << 32), leaf)


def cfgi_ste_range(sid, rng):
    """Covers the 2^(rng+1) StreamIDs that share sid's bits above rng."""
    return (CMD_CFGI_STE_RANGE | (sid << 32), rng)


def cfgi_all():
    return cfgi_ste_range(0, 31)


def cfgi_cd(sid, leaf=1):
    return (CMD_CFGI_CD | (sid << 32), leaf)


def cfgi_cd_all(sid):
    return (CMD_CFGI_CD_ALL | (sid << 32), 0)


def tlbi_nh_all():
    return (CMD_TLBI_NH_ALL, 0)


def tlbi_nsnh_all():
    return (CMD_TLBI_NSNH_ALL, 0)


def tlbi_nh_asid(asid):
    return (CMD_TLBI_NH_ASID | (asid << 48), 0)


def tlbi_nh_va(asid, va, leaf=1):
    return (CMD_TLBI_NH_VA | (asid << 48), (va & PAGE_MASK) | leaf)


def tlbi_nh_vaa(va, leaf=1):
    return (CMD_TLBI_NH_VAA, (va & PAGE_MASK) | leaf)


@dataclass
class Event:
    id: int
    sid: int
    addr: int = 0
    read: bool = False           # RnW
    instruction: bool = False    # InD
    privileged: bool = False     # PnU
    raw: tuple = ()

    @classmethod
    def parse(cls, words):
        w0, w1, w2, _ = words
        return cls(id=w0 & 0xFF, sid=w0 >> 32, addr=w2,
                   read=bool((w1 >> 35) & 1), instruction=bool((w1 >> 34) & 1),
                   privileged=bool((w1 >> 33) & 1), raw=tuple(words))

    def fields(self):
        return (self.id, self.sid, self.addr, self.read, self.instruction, self.privileged)
