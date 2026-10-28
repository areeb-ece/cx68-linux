#!/usr/bin/env node
// Launch app.qmk.top in headed Chromium with the WebHID logger injected.
//
//   node tools/capture/capture.mjs [--mode readonly|write] [--out captures/x.jsonl] [--url URL]
//
// Every WebHID call is appended to the JSONL file and echoed here. Type a line in this
// terminal and press Enter to drop a {"type":"mark"} entry (e.g. "set actuation W 1.2mm")
// so captures can be diffed per action. Ctrl+C or closing the browser ends the session.
import { chromium } from "playwright";
import fs from "node:fs";
import path from "node:path";
import readline from "node:readline";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "../..");
const args = Object.fromEntries(
  process.argv.slice(2).reduce((acc, a, i, all) => (a.startsWith("--") ? [...acc, [a.slice(2), all[i + 1]]] : acc), []),
);
const mode = args.mode ?? "readonly";
if (!["readonly", "write"].includes(mode)) throw new Error(`--mode must be readonly or write, got ${mode}`);
const url = args.url ?? "https://app.qmk.top/";
const stamp = new Date().toISOString().replace(/[:.]/g, "-");
const outPath = path.resolve(args.out ?? path.join(root, "captures", `${stamp}-${mode}.jsonl`));
fs.mkdirSync(path.dirname(outPath), { recursive: true });
const out = fs.createWriteStream(outPath, { flags: "a" });

const commands = JSON.parse(fs.readFileSync(path.join(here, "commands.json"), "utf8"));
const logger = fs
  .readFileSync(path.join(here, "hid-logger.js"), "utf8")
  .replace("/*@@CONFIG@@*/ null", JSON.stringify({ mode, commands }));

const c = { dim: "\x1b[2m", red: "\x1b[31m", green: "\x1b[32m", yellow: "\x1b[33m", cyan: "\x1b[36m", reset: "\x1b[0m" };
const nameOf = (hex) => {
  const code = "0x" + hex.slice(0, 2).toUpperCase();
  return commands.commands[code]?.name ?? commands.deny[code] ?? "UNKNOWN";
};
const short = (hex, n = 24) => (hex.length > n * 2 ? hex.slice(0, n * 2).replace(/(..)/g, "$1 ") + "…" : hex.replace(/(..)/g, "$1 "));

const write = (e) => out.write(JSON.stringify(e) + "\n");
const show = (e) => {
  const t = new Date(e.t).toISOString().slice(11, 23);
  switch (e.type) {
    case "setFeature":
      return console.log(
        `${t} ${e.blocked ? c.red + "BLOCK" : c.green + "SET  "}${c.reset} dev${e.dev} [${nameOf(e.data)}] ${short(e.data)}` +
          (e.blocked ? `\n             ${c.red}${e.blocked}${c.reset}` : ""),
      );
    case "getFeature":
      return console.log(`${t} ${c.cyan}GET  ${c.reset} dev${e.dev} [${nameOf(e.data)}] ${short(e.data)}`);
    case "input":
      return console.log(`${t} ${c.yellow}IN   ${c.reset} dev${e.dev} id${e.reportId} ${short(e.data)}`);
    case "mark":
      return console.log(`${c.yellow}──── ${e.label} ────${c.reset}`);
    case "out":
      return console.log(`${t} ${c.red}BLOCK${c.reset} output report id${e.reportId} ${short(e.data)}  ${e.blocked}`);
    default:
      return console.log(`${t} ${c.dim}${e.type} ${JSON.stringify({ ...e, type: undefined, seq: undefined, t: undefined })}${c.reset}`);
  }
};
const record = (e) => {
  write(e);
  show(e);
};

const context = await chromium.launchPersistentContext(path.join(here, "profile"), {
  headless: false,
  viewport: null,
  args: ["--start-maximized"],
});
await context.exposeBinding("__hidlog", (_src, json) => {
  const e = JSON.parse(json);
  e.host_t = Date.now();
  record(e);
});
await context.addInitScript({ content: logger });
const page = context.pages()[0] ?? (await context.newPage());
page.on("console", (m) => m.type() === "error" && write({ type: "console.error", t: Date.now(), text: m.text() }));

record({ type: "session", t: Date.now(), mode, url, out: outPath });
console.log(`${c.dim}logging to ${outPath}\ntype a label + Enter to mark an action; Ctrl+C to quit${c.reset}`);
await page.goto(url);

const rl = readline.createInterface({ input: process.stdin });
rl.on("line", (label) => label.trim() && record({ type: "mark", t: Date.now(), label: label.trim() }));

const finish = async () => {
  rl.close();
  await context.close().catch(() => {});
  out.end(() => process.exit(0));
};
context.on("close", finish);
process.on("SIGINT", finish);
