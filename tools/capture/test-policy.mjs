// Offline test of hid-logger.js's safety policy: runs the real injected script in a VM
// against a fake WebHID device and checks which feature reports reach the "device".
//   node tools/capture/test-policy.mjs
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const commands = JSON.parse(fs.readFileSync(path.join(here, "commands.json"), "utf8"));
const src = fs.readFileSync(path.join(here, "hid-logger.js"), "utf8");

// 64-byte report with Bit7 (or Bit8) checksum, as ___encodeCmd builds it.
const pkt = (bytes, bit8 = false) => {
  const b = new Uint8Array(64);
  b.set(bytes);
  const n = bit8 ? 8 : 7;
  b[n] = 0xff - (b.slice(0, n).reduce((a, x) => a + x, 0) & 0xff);
  return b;
};

function load(mode) {
  const sent = [];
  const logs = [];
  class HIDDevice {
    constructor() {
      this.vendorId = 0x3151;
      this.productId = 0x502d;
      this.productName = "Wired Keyboard";
      this.collections = [];
    }
    addEventListener() {}
    async open() {}
    async close() {}
    async sendReport(id, d) {
      sent.push(["out", id, d]);
    }
    async sendFeatureReport(id, d) {
      sent.push(["feature", id, d]);
    }
    async receiveFeatureReport() {
      return new DataView(new ArrayBuffer(64));
    }
  }
  class HIDProto {
    async requestDevice() {
      return [];
    }
    async getDevices() {
      return [];
    }
    addEventListener() {}
  }
  const hid = new HIDProto();
  const window = { __hidlog: (s) => logs.push(JSON.parse(s)) };
  const ctx = vm.createContext({
    window,
    navigator: { hid },
    HIDDevice,
    DOMException: class extends Error {},
    performance: { timeOrigin: 0, now: () => 0 },
    location: { href: "test" },
    Uint8Array,
    ArrayBuffer,
  });
  vm.runInContext(src.replace("/*@@CONFIG@@*/ null", JSON.stringify({ mode, commands })), ctx);
  return { dev: new HIDDevice(), sent, logs };
}

async function attempt(mode, data, reportId = 0) {
  const { dev, sent, logs } = load(mode);
  const ok = await dev.sendFeatureReport(reportId, data).then(
    () => true,
    () => false,
  );
  assert.equal(ok, sent.length === 1, "forwarded iff promise resolved");
  return { ok, log: logs.find((l) => l.type === "setFeature") };
}

const cases = [
  // [description, mode, bytes, bit8, expected forwarded]
  ["GET_USB_VERSION read", "readonly", [0x8f], false, true],
  ["GET_MULTI_MAGNETISM actuation page 0", "readonly", [0xe5, 0, 1, 0], false, true],
  ["GET_LEDPARAM", "readonly", [0x87], false, true],
  ["SET_TRAVEL_REPORT in readonly", "readonly", [0x1b, 1], false, false],
  ["SET_TRAVEL_REPORT in write mode", "write", [0x1b, 1], false, true],
  ["SET_LEDPARAM with Bit7 checksum", "write", [0x07, 1, 2, 3], false, true],
  ["GET_BATTERY read", "readonly", [0x82], false, true],
  ["factory reset 0x01", "write", [0x01], false, false],
  ["factory reset 0x02 (older class)", "write", [0x02], false, false],
  ["USB bootloader 7F 55 AA 55 AA", "write", [0x7f, 0x55, 0xaa, 0x55, 0xaa], false, false],
  ["RF bootloader F8 55 AA 55 AA", "write", [0xf8, 0x55, 0xaa, 0x55, 0xaa], false, false],
  ["upgrade start BA C0", "write", [0xba, 0xc0, 1, 0], false, false],
  ["flash chip erase 0xAC", "write", [0xac], false, false],
  ["min calibration 0x1C", "write", [0x1c, 1], false, false],
  ["max calibration 0x1E", "write", [0x1e, 1], false, false],
  ["unknown opcode 0x50", "write", [0x50], false, false],
  ["unknown high opcode 0xD0", "readonly", [0xd0], false, false],
];

let failed = 0;
for (const [desc, mode, bytes, bit8, expect] of cases) {
  const { ok, log } = await attempt(mode, pkt(bytes, bit8));
  const pass = ok === expect;
  failed += !pass;
  console.log(`${pass ? "ok  " : "FAIL"} ${desc}: ${ok ? "forwarded" : "blocked (" + log.blocked + ")"}`);
}

// Raw firmware chunk: valid opcode byte but no checksum, as ___upgradeFirmware sends.
{
  const raw = new Uint8Array(64).fill(0x5a);
  raw[0] = 0x8f;
  const { ok } = await attempt("write", raw);
  failed += ok;
  console.log(`${ok ? "FAIL" : "ok  "} unchecksummed chunk starting with a read opcode: ${ok ? "forwarded" : "blocked"}`);
}
// Known write opcode with a corrupt checksum byte.
{
  const b = pkt([0x07, 1, 2, 3]);
  b[7] ^= 0x01;
  const { ok } = await attempt("write", b);
  failed += ok;
  console.log(`${ok ? "FAIL" : "ok  "} SET_LEDPARAM with corrupt checksum: ${ok ? "forwarded" : "blocked"}`);
}
// Wrong report ID.
{
  const { ok } = await attempt("write", pkt([0x8f]), 5);
  failed += ok;
  console.log(`${ok ? "FAIL" : "ok  "} feature report id 5: ${ok ? "forwarded" : "blocked"}`);
}
// Output reports are always refused.
{
  const { dev, sent } = load("write");
  const ok = await dev.sendReport(6, pkt([0x8f])).then(() => true, () => false);
  failed += ok || sent.length > 0;
  console.log(`${ok ? "FAIL" : "ok  "} output report: ${ok ? "forwarded" : "blocked"}`);
}

if (failed) {
  console.error(`${failed} policy test(s) failed`);
  process.exit(1);
}
console.log("all policy tests passed");
