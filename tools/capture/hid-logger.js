// Injected into every page before the app's own scripts (Playwright addInitScript).
// Wraps the WebHID API, logs every call/result/input report through window.__hidlog,
// and refuses to forward feature reports that the safety policy rejects.
// capture.mjs substitutes the config placeholder below with { mode, commands } before injection.
(() => {
  if (!("hid" in navigator) || window.__hidLoggerInstalled) return;
  window.__hidLoggerInstalled = true;

  const CONFIG = /*@@CONFIG@@*/ null;
  const COMMANDS = CONFIG.commands.commands;
  const DENY = CONFIG.commands.deny;
  const BIT8 = new Set(CONFIG.commands.checksum.bit8_opcodes);

  let seq = 0;
  const now = () => performance.timeOrigin + performance.now();
  const hex = (u8) => Array.from(u8, (b) => b.toString(16).padStart(2, "0")).join("");
  const op = (b) => "0x" + b.toString(16).toUpperCase().padStart(2, "0");
  const toU8 = (data) =>
    data instanceof ArrayBuffer
      ? new Uint8Array(data)
      : new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
  const emit = (e) => {
    try {
      window.__hidlog(JSON.stringify({ seq: seq++, t: now(), ...e }));
    } catch {}
  };

  const ids = new WeakMap();
  let nextId = 0;
  const devId = (d) => {
    if (!ids.has(d)) ids.set(d, nextId++);
    return ids.get(d);
  };
  const describe = (d) => ({
    dev: devId(d),
    vid: d.vendorId,
    pid: d.productId,
    name: d.productName,
    collections: d.collections.map((c) => ({
      usagePage: c.usagePage,
      usage: c.usage,
      input: c.inputReports.map((r) => r.reportId),
      output: c.outputReports.map((r) => r.reportId),
      feature: c.featureReports.map((r) => r.reportId),
    })),
  });

  const checksumOk = (b) => {
    const p = new Uint8Array(Math.max(9, b.length));
    p.set(b);
    const sum = (n) => p.slice(0, n).reduce((a, x) => a + x, 0) & 0xff;
    return { bit7: p[7] === 0xff - sum(7), bit8: p[8] === 0xff - sum(8) };
  };

  // Returns null if allowed, otherwise the reason it is blocked.
  const policy = (reportId, b) => {
    if (reportId !== 0) return `feature report id ${reportId} is not the config channel`;
    const code = op(b[0]);
    if (DENY[code]) return `denylisted ${code}: ${DENY[code]}`;
    const cmd = COMMANDS[code];
    if (!cmd) return `unknown opcode ${code} (purpose not confirmed)`;
    const ck = checksumOk(b);
    if (!(BIT8.has(code) ? ck.bit8 : ck.bit7)) return `bad checksum for ${code} (bit7=${ck.bit7} bit8=${ck.bit8})`;
    if (CONFIG.mode === "readonly" && cmd.kind !== "read") return `${code} ${cmd.name} is ${cmd.kind}; capture is read-only`;
    return null;
  };

  const blocked = (reason) => Promise.reject(new DOMException(`blocked by capture proxy: ${reason}`, "NotAllowedError"));

  const listening = new WeakSet();
  const listen = (d) => {
    if (listening.has(d)) return;
    listening.add(d);
    d.addEventListener("inputreport", (e) =>
      emit({ type: "input", dev: devId(d), reportId: e.reportId, data: hex(toU8(e.data)) }),
    );
  };

  const HID = Object.getPrototypeOf(navigator.hid);
  const origRequest = HID.requestDevice;
  HID.requestDevice = async function (options) {
    emit({ type: "requestDevice", filters: options?.filters ?? [] });
    const devs = await origRequest.call(this, options);
    emit({ type: "requestDevice:result", devices: devs.map(describe) });
    return devs;
  };
  const origGet = HID.getDevices;
  HID.getDevices = async function () {
    const devs = await origGet.call(this);
    emit({ type: "getDevices:result", devices: devs.map(describe) });
    return devs;
  };
  navigator.hid.addEventListener("connect", (e) => emit({ type: "connect", device: describe(e.device) }));
  navigator.hid.addEventListener("disconnect", (e) => emit({ type: "disconnect", dev: devId(e.device) }));

  const P = HIDDevice.prototype;
  const { open, close, sendReport, sendFeatureReport, receiveFeatureReport } = P;

  P.open = async function () {
    emit({ type: "open", device: describe(this) });
    const r = await open.call(this);
    listen(this);
    return r;
  };
  P.close = async function () {
    emit({ type: "close", dev: devId(this) });
    return close.call(this);
  };
  P.sendReport = function (reportId, data) {
    const b = toU8(data);
    const reason = "output reports are never used over USB";
    emit({ type: "out", dev: devId(this), reportId, data: hex(b), blocked: reason });
    return blocked(reason);
  };
  P.sendFeatureReport = async function (reportId, data) {
    const b = toU8(data);
    const reason = policy(reportId, b);
    emit({ type: "setFeature", dev: devId(this), reportId, data: hex(b), ...(reason && { blocked: reason }) });
    if (reason) return blocked(reason);
    try {
      return await sendFeatureReport.call(this, reportId, data);
    } catch (err) {
      emit({ type: "setFeature:error", dev: devId(this), error: String(err) });
      throw err;
    }
  };
  P.receiveFeatureReport = async function (reportId) {
    try {
      const dv = await receiveFeatureReport.call(this, reportId);
      emit({ type: "getFeature", dev: devId(this), reportId, data: hex(toU8(dv)) });
      return dv;
    } catch (err) {
      emit({ type: "getFeature:error", dev: devId(this), reportId, error: String(err) });
      throw err;
    }
  };
  emit({ type: "installed", mode: CONFIG.mode, url: location.href });
})();
