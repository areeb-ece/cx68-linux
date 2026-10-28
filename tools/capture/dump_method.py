"""Print a condensed body of a named method from a class chain in the pretty bundle.

    python3 -I tools/capture/dump_method.py <main.pretty.js> <methodName> [startLine]

Strips regenerator boilerplate (case labels, t.next bookkeeping) so the packet-building
statements are easy to read. startLine picks among same-named methods (from class_chain.py).
"""
import re
import sys

L = open(sys.argv[1], encoding="utf-8").read().split("\n")
name = sys.argv[2]
start = int(sys.argv[3]) - 1 if len(sys.argv) > 3 else next(
    i for i, l in enumerate(L) if re.search(rf'"{re.escape(name)}",\s*$|\(e\),\s*"{re.escape(name)}",', l))
indent = len(L[start]) - len(L[start].lstrip())
skip = re.compile(r"^\s*(case \d+:|case \"end\":|default:|break;|for \(;;\)|switch \(\(\w\.prev = \w\.next\)\) \{|\}\)?[,;)]*$|"
                  r"return \w\.stop\(\);|\w\.next = \d+;|var [a-z, ]+;|return function|\};?$)")
out = []
for l in L[start + 1:]:
    ind = len(l) - len(l.lstrip())
    if l.strip() and ind <= indent and not l.strip().startswith((")", "}", ".")):
        break
    if l.strip() and not skip.match(l):
        out.append(re.sub(r"\(\w\.next = \d+\),\s*", "", l.strip()))
print(f"--- {name} @ {start + 1}")
print("\n".join(out[:120]))
