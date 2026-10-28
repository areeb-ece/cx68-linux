"""Re-export of cx68.policy so the capture tools and the library share one policy."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from cx68.policy import *  # noqa: E402,F401,F403
from cx68.policy import BIT8_OPCODES, COMMANDS, DENY, NONE_OPCODES  # noqa: E402,F401
