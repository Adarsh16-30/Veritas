#!/usr/bin/env python
"""Assert no ERPNext write is retried at the HTTP layer (Rule 5).

A write whose response was lost may already have happened. Resending it blind
can post a second document; only the idempotency key may decide
(``submit_once`` and the pipeline's orphan adoption). ``ERPClient`` funnels
every request through ``_send(verb, url, *, retry=...)``. This script parses
``erp/client.py`` and requires that:

* every ``_send`` call states ``retry`` explicitly;
* every ``post``/``put`` passes ``retry=False``, except ``call``, which may pass
  its ``idempotent`` flag; that flag defaults to ``False``, and is set only for
  the read-only document mappers.

Exit code 0 = writes are sent once; 1 = a write could be retried.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

CLIENT = Path(__file__).resolve().parent.parent / "erp" / "client.py"
WRITE_VERBS = {"post", "put", "delete"}


def problems(source: str) -> list[str]:
    tree = ast.parse(source)
    found: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        if fn.name == "call":
            defaults = dict(
                zip(
                    [a.arg for a in fn.args.args][-len(fn.args.defaults) :],
                    fn.args.defaults,
                    strict=True,
                )
            )
            flag = defaults.get("idempotent")
            if not (isinstance(flag, ast.Constant) and flag.value is False):
                found.append("call(): idempotent must default to False")
        for node in ast.walk(fn):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_send"
            ):
                continue
            verb = (
                node.args[0].value if node.args and isinstance(node.args[0], ast.Constant) else None
            )
            retry = next((k.value for k in node.keywords if k.arg == "retry"), None)
            where = f"{fn.name}() line {node.lineno}"
            if retry is None:
                found.append(f"{where}: _send without an explicit retry=")
            elif verb in WRITE_VERBS:
                is_false = isinstance(retry, ast.Constant) and retry.value is False
                is_flag = (
                    fn.name == "call" and isinstance(retry, ast.Name) and retry.id == "idempotent"
                )
                if not (is_false or is_flag):
                    found.append(f"{where}: {verb} may be retried ({ast.unparse(retry)})")
    return found


def main() -> int:
    found = problems(CLIENT.read_text(encoding="utf-8"))
    for p in found:
        print(p)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
