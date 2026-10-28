"""Resolve a Babel-transpiled class's inheritance chain and effective FEA_CMD_* table
in the pretty-printed iotdriver bundle.

    python3 -I tools/capture/class_chain.py <main.pretty.js> <ClassVar>

Prints the chain (subclass first) and every FEA_CMD constant with the class that wins
(subclass constructors run after the parent's, so the most-derived assignment wins).
"""
import re
import sys

src_lines = open(sys.argv[1], encoding="utf-8").read().split("\n")
src = "\n".join(src_lines)


def class_span(name):
    # Babel output: `NAME = (function (e) { ... })(PARENT)` ; find start line and parent.
    m = re.search(rf"\b{re.escape(name)} = \(function \((\w+)\) \{{", src)
    if not m:
        return None
    start = src.count("\n", 0, m.start())
    depth, i = 0, m.end() - 1
    while True:
        c = src[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    tail = re.match(r"\}\)\(([\w$]+)\)", src[i:i + 80])
    end = src.count("\n", 0, i)
    return start, end, tail.group(1) if tail else None


chain, name = [], sys.argv[2]
while name:
    span = class_span(name)
    if span is None:
        chain.append((name, None, None))
        break
    chain.append((name, span[0], span[1]))
    name = span[2]
print("chain:", " -> ".join(n for n, *_ in chain))

effective = {}
for name, lo, hi in reversed(chain):  # base first, derived overrides
    if lo is None:
        continue
    for ln in range(lo, hi + 1):
        for m in re.finditer(r'"(FEA_CMD_[A-Z0-9_]+)",\s*(\d+)\)', src_lines[ln]):
            effective[m.group(1)] = (int(m.group(2)), name, ln + 1)
if "--methods" in sys.argv:
    # Methods are defined as HELPER(HELPER2(e), "name", ...) inside the constructor.
    method_re = re.compile(r'^\s*[\w$]+\(\s*[\w$]+\(e\),\s*"([^"]+)",')
    methods = {}
    for name, lo, hi in reversed(chain):
        if lo is None:
            continue
        starts = [(ln, m.group(1)) for ln in range(lo, hi + 1) if (m := method_re.match(src_lines[ln]))]
        # A method definition may also span two lines: HELPER(\n PHe(e),\n "name",
        for ln in range(lo, hi):
            if re.match(r'^\s*[\w$]+\($', src_lines[ln]) and re.match(r'^\s*[\w$]+\(e\),$', src_lines[ln + 1]):
                m = re.match(r'^\s*"([^"]+)",', src_lines[ln + 2])
                if m:
                    starts.append((ln, m.group(1)))
        starts.sort()
        for i, (ln, mname) in enumerate(starts):
            end = starts[i + 1][0] if i + 1 < len(starts) else hi
            body = "\n".join(src_lines[ln:end])
            if mname.startswith("FEA_CMD_"):
                continue
            ops = sorted(set(re.findall(r"FEA_CMD_[A-Z0-9_]+", body)))
            lits = sorted(set(re.findall(r"\[0\] = (\d+)\b", body)))
            cks = sorted(set(re.findall(r"CheckSumType\.(BIT7|BIT8|NONE)", body)))
            bare = len(re.findall(r"writeFeatureCmd\([\w$]+\)", body))
            methods[mname] = (name, ln + 1, end - ln, ops, lits, cks, bare)
    for mname, (cls, ln, n, ops, lits, cks, bare) in sorted(methods.items(), key=lambda x: x[1][1]):
        if ops or lits:
            print(f"{cls:>4}:{ln:<7} {mname:<40} {' '.join(o.replace('FEA_CMD_', '') for o in ops)} "
                  f"{('lit:' + ','.join(lits)) if lits else ''} ck:{'/'.join(cks) or '-'}{' bareNONE:' + str(bare) if bare else ''}")
    sys.exit()

byval = {}
for k, (v, cls, ln) in effective.items():
    byval.setdefault(v, []).append(f"{k}[{cls}:{ln}]")
for v in sorted(byval):
    print(f"0x{v:02X}  " + ", ".join(byval[v]))
