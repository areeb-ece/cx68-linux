"""Active profile switch (0x04, from Uga.setCurrentProfile: [04, profile 0..3])."""
from . import protocol as P

WRITES = (0x04,)


class VerifyError(Exception):
    """The keyboard does not read back what was written."""


def switch(dev, profile: int) -> int:
    """Make `profile` (0-3) active and verify. dev must allow 0x04."""
    if not 0 <= profile < P.PROFILES:
        raise ValueError(f"profile must be 0-{P.PROFILES - 1}")
    dev.transact(*P.set_profile(profile))
    dev.wait_ready()
    got = dev.transact(*P.get_profile())[1]
    if got != profile:
        raise VerifyError(f"active profile reads back as {got + 1}, wanted {profile + 1}")
    return got
