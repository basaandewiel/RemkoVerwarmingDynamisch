#!/usr/bin/env python3
"""Vergelijk twee registerdumps (van `dump_registers.py`) en toon wat veranderde.

Om te ontdekken welk register de GUI-instelling "vaste waarde" van de VK-modus
draagt: maak een dump vóór en ná het verzetten van de vaste waarde in de GUI en
laat dit script de verschillen zien. Registers die alleen door ruis (temperaturen,
debiet) veranderen kun je met `--ignore` wegfilteren.

Gebruik:
  python3 compare_dumps.py a.json b.json
  python3 compare_dumps.py a.json b.json --ignore 5032,5582,5132
  python3 compare_dumps.py a.json b.json --ids-only
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional


def load(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def decode_temp(raw: str) -> Optional[float]:
    """Probeer een 16-bits hex-waarde als temperatuur (÷10) te duiden."""
    try:
        v = int(str(raw), 16)
        if v >= 0x8000:
            v -= 0x10000
        if abs(v) <= 600:
            return v / 10.0
    except (ValueError, TypeError):
        pass
    return None


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="Vergelijk twee Remko-registerdumps")
    ap.add_argument("first")
    ap.add_argument("second")
    ap.add_argument("--ignore", default="",
                    help="komma-lijst van registers die je wilt negeren (ruis)")
    ap.add_argument("--ids-only", action="store_true",
                    help="alleen de register-id's tonen")
    args = ap.parse_args(argv)

    a = load(args.first)
    b = load(args.second)
    ignore = {s.strip() for s in args.ignore.split(",") if s.strip()}

    ids = sorted(set(a) | set(b), key=lambda x: int(x) if str(x).isdigit() else 0)
    changed = []
    for reg in ids:
        if reg in ignore:
            continue
        va, vb = a.get(reg), b.get(reg)
        if va != vb:
            changed.append((reg, va, vb))

    print(f"=== {args.first}  vs  {args.second} ===")
    print(f"registers: {len(a)} / {len(b)};  verschillen: {len(changed)}")
    print()

    if not changed:
        print("Geen verschillen — bewoog er niets? (of verkeerde bestanden)")
        return 0

    if args.ids_only:
        print("gewijzigde registers:", ", ".join(r for r, _, _ in changed))
        return 0

    print(f"{'reg':>6}  {'oud':>8}  {'nieuw':>8}   temp oud -> nieuw")
    for reg, va, vb in changed:
        ta, tb = decode_temp(va), decode_temp(vb)
        hint = ""
        if ta is not None and tb is not None:
            hint = f"   {ta:g} °C -> {tb:g} °C"
        print(f"{reg:>6}  {str(va):>8}  {str(vb):>8}{hint}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
