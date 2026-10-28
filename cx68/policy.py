"""Safety policy for the CX68 (Rongyuan RY5088) config channel.

Single source of truth is commands.json next to this file; hid-logger.js implements the
same rules for the WebHID path. A packet is the 64-byte feature report payload (no report
ID byte). Everything not explicitly known is refused.
"""
import json
from pathlib import Path

COMMANDS_FILE = Path(__file__).with_name("commands.json")
REPORT_LEN = 64

BIT7, BIT8, NONE = 0, 1, 2  # proto.driver.CheckSumType


def load_table(path=COMMANDS_FILE):
    t = json.loads(Path(path).read_text())
    commands = {int(k, 16): v for k, v in t["commands"].items()}
    deny = {int(k, 16): v for k, v in t["deny"].items()}
    bit8 = {int(k, 16) for k in t["checksum"]["bit8_opcodes"]}
    none = {int(k, 16) for k in t["checksum"].get("none_opcodes", [])}
    return commands, deny, bit8, none


COMMANDS, DENY, BIT8_OPCODES, NONE_OPCODES = load_table()
CHECKSUM_NAMES = {BIT7: "BIT7", BIT8: "BIT8", NONE: "NONE"}


def checksum_ok(packet: bytes):
    p = bytes(packet).ljust(9, b"\0")
    return (p[7] == 0xFF - (sum(p[:7]) & 0xFF), p[8] == 0xFF - (sum(p[:8]) & 0xFF))


def encode(msg: bytes, checksum_type: int) -> bytes:
    """Build the 64-byte report exactly as the vendor app's ___encodeCmd + padding does."""
    if len(msg) > REPORT_LEN:
        raise ValueError(f"message is {len(msg)} bytes, max {REPORT_LEN}")
    p = bytearray(msg.ljust(9, b"\0"))
    if checksum_type == BIT7:
        p[7] = 0xFF - (sum(p[:7]) & 0xFF)
    elif checksum_type == BIT8:
        p[8] = 0xFF - (sum(p[:8]) & 0xFF)
    return bytes(p.ljust(REPORT_LEN, b"\0"))


def name(opcode: int) -> str:
    if opcode in COMMANDS:
        return COMMANDS[opcode]["name"]
    if opcode in DENY:
        return "DENY:" + DENY[opcode].split(" (")[0]
    return "UNKNOWN"


def expected_checksum(op: int) -> int:
    return BIT8 if op in BIT8_OPCODES else NONE if op in NONE_OPCODES else BIT7


def validate_allow(opcodes):
    """Opcodes that may be enabled individually in read-only mode: volatile ones only."""
    opcodes = list(opcodes)  # may be a generator; it is iterated twice below
    for op in opcodes:
        cmd = COMMANDS.get(op)
        if cmd is None or op in DENY or cmd["kind"] != "volatile":
            raise ValueError(f"0x{op:02X} is not a known volatile command and cannot be allowed individually")
    return frozenset(opcodes)


def validate_allow_writes(opcodes):
    """Write opcodes individually permitted in read-only mode. Never denylisted ones."""
    opcodes = list(opcodes)
    for op in opcodes:
        cmd = COMMANDS.get(op)
        if cmd is None or op in DENY or cmd["kind"] != "write":
            raise ValueError(f"0x{op:02X} is not a known write command and cannot be allowed individually")
    return frozenset(opcodes)


def check(packet: bytes, mode: str, checksum_type: int | None = None, allow=frozenset()) -> str | None:
    """Return None if the packet may be sent, else the reason it is blocked.

    mode: "readonly" allows only known reads; "write" also allows known writes.
    allow: volatile opcodes additionally permitted in read-only mode (see validate_allow).
    checksum_type: what the caller asked for (the helper protocol states it); None when
    unknown (WebHID), in which case the packet's own checksum is validated.
    """
    if mode not in ("readonly", "write"):
        raise ValueError(mode)
    if len(packet) != REPORT_LEN:
        return f"packet is {len(packet)} bytes, expected {REPORT_LEN}"
    op = packet[0]
    if op in DENY:
        return f"denylisted 0x{op:02X}: {DENY[op]}"
    cmd = COMMANDS.get(op)
    if cmd is None:
        return f"unknown opcode 0x{op:02X} (purpose not confirmed)"
    want = expected_checksum(op)
    if checksum_type is not None and checksum_type != want:
        return (f"0x{op:02X} sent with checksum {CHECKSUM_NAMES.get(checksum_type, checksum_type)}, "
                f"expected {CHECKSUM_NAMES[want]}")
    if want != NONE:
        bit7, bit8 = checksum_ok(packet)
        if not (bit8 if want == BIT8 else bit7):
            return f"bad checksum for 0x{op:02X} (bit7={bit7} bit8={bit8})"
    if mode == "readonly" and cmd["kind"] != "read" and op not in allow:
        return f"0x{op:02X} {cmd['name']} is {cmd['kind']}; mode is read-only"
    return None
