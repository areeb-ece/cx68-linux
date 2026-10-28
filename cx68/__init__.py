"""Linux library for the iCAN CX68 (CamelTech CT-M68, Rongyuan RY5088) Hall-effect keyboard.

    from cx68 import Device, config, live
    with Device() as dev:                 # read-only by default
        cfg = config.read_full_config(dev)
        for frame in live.poll(dev): ...

Every packet passes policy.check (cx68/commands.json); see docs/protocol.md.
"""
from .transport import CX68Error, Device, DeviceBusy, DeviceGone, NoReply, NotFound, Refused, find

__all__ = ["CX68Error", "Device", "DeviceBusy", "DeviceGone", "NoReply", "NotFound", "Refused", "find"]
