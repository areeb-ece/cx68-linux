#!/usr/bin/env bash
# Ground-truth USB capture of the CX68 config traffic, to cross-check the WebHID log.
#
#   tools/capture/usbmon.sh [out.pcapng]      (Ctrl+C to stop)
#
# Needs: `sudo modprobe usbmon`, wireshark-cli, and membership of the wireshark group
# (or run as root). The bus also carries every keystroke and mouse movement, so the raw
# capture goes to a temporary file that is deleted on exit. Only the keyboard's
# endpoint-0 control transfers (feature reports) and interface-1 report-5 notifications
# are kept. Typing (NKRO report 1 / boot keyboard) is dropped.
set -euo pipefail

VID=3151 PID=502d
here="$(cd "$(dirname "$0")" && pwd)"
out="${1:-$here/../../captures/$(date +%Y-%m-%dT%H-%M-%S)-usbmon.pcapng}"
mkdir -p "$(dirname "$out")"

dev=""
for d in /sys/bus/usb/devices/*; do
  [[ -f $d/idVendor && $(<"$d/idVendor") == "$VID" && $(<"$d/idProduct") == "$PID" ]] && dev=$d
done
[[ -n $dev ]] || { echo "keyboard $VID:$PID not found" >&2; exit 1; }
bus=$(<"$dev/busnum") addr=$(<"$dev/devnum")
[[ -e /sys/kernel/debug/usb/usbmon || -d /sys/module/usbmon ]] || { echo "usbmon not loaded: sudo modprobe usbmon" >&2; exit 1; }

raw="$(mktemp --suffix=.pcapng)"
trap 'rm -f "$raw"' EXIT
echo "capturing usbmon$bus (keyboard at address $addr); Ctrl+C to stop" >&2
tshark -q -i "usbmon$bus" -w "$raw" || true   # Ctrl+C ends tshark; filter afterwards

# Endpoint 0 (control: SET/GET_REPORT feature) both directions, plus EP 0x82 IN frames whose
# HID payload starts with report ID 5 (vendor notifications).
filter="usb.bus_id == $bus && usb.device_address == $addr && (usb.endpoint_address.number == 0 || (usb.endpoint_address == 0x82 && usb.capdata[0] == 05))"
tshark -r "$raw" -Y "$filter" -w "$out"
echo "kept $(tshark -r "$out" | wc -l) packets -> $out" >&2
