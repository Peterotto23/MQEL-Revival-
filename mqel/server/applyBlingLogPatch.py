#!/usr/bin/env python3
"""Adds one log line per bling-drop roll, so the live server shows what happens.

Run from the repo root (the folder with server/), then RESTART the game server:
    python3 apply_bling_log_patch.py           # apply
    python3 apply_bling_log_patch.py --check   # only test that the edits match
Log lines (game server output / journal) start with "[bling-drop]":
    skip ...   nothing rolled (economy off, PvP/PvE switch off, no creatures)
    miss ...   rolled, no drop this attack
    drop ...   a creature carries blings
    end  ...   at the end of the attack: carried vs. credited (0 credited with
               carried > 0 means the carrier creature was not killed)
Keeps each file's line endings; no-op when run twice.
"""
import sys

EDITS = [
("server/loot.py", '''def add_bling_drop(creature_loot, account, now, rng=random, pvp=None):''',
'''def _bling_log(msg):
    import sys
    print("[bling-drop] " + msg, file=sys.stderr, flush=True)


def add_bling_drop(creature_loot, account, now, rng=random, pvp=None):'''),
("server/loot.py", '''    if not balance.enabled() or not creature_loot or not bling_drop_allowed(pvp):
        return
    if rng.random() * 100 >= balance.get("Loot", "Chance"):
        return
    amount = balance.get("Loot", "Amount")
    left = bling_loot_left(account, now)
    if left is not None:
        amount = min(amount, left)
    if amount > 0:
        creature_loot[rng.choice(sorted(creature_loot))]["PremiumCash"] = amount
''', '''    allowed = bling_drop_allowed(pvp)
    if not balance.enabled() or not creature_loot or not allowed:
        _bling_log(f"skip enabled={balance.enabled()} creatures={len(creature_loot or {})} "
                   f"pvp={pvp} allowed={allowed}")
        return
    roll = rng.random() * 100
    if roll >= balance.get("Loot", "Chance"):
        _bling_log(f"miss roll={roll:.1f} chance={balance.get('Loot', 'Chance')} pvp={pvp}")
        return
    amount = balance.get("Loot", "Amount")
    left = bling_loot_left(account, now)
    if left is not None:
        amount = min(amount, left)
    if amount > 0:
        creature_loot[rng.choice(sorted(creature_loot))]["PremiumCash"] = amount
    _bling_log(f"drop amount={amount} pvp={pvp} weekly_left={left}")
'''),
("server/attack.py", '''    kills_blings = _sum(cur, "PremiumCash", params.get("KilledCreatureIds"))
''', '''    kills_blings = _sum(cur, "PremiumCash", params.get("KilledCreatureIds"))
    carried = sum(x.get("PremiumCash", 0) for x in (cur.get("loot") or {}).values())
    if carried or kills_blings:
        loot._bling_log(f"end carried={carried} credited={kills_blings} "
                        f"killed={len(params.get('KilledCreatureIds') or [])}")
'''),
]


def main():
    check = "--check" in sys.argv
    out = {}
    for path, old, new in EDITS:
        if path not in out:
            with open(path, encoding="utf-8", newline="") as f:
                raw = f.read()
            out[path] = ("\r\n" in raw, raw.replace("\r\n", "\n"))
        crlf, text = out[path]
        if new in text:
            continue  # already applied
        if text.count(old) != 1:
            sys.exit(f"{path}: edit does not match (wrong file version?); nothing written")
        out[path] = (crlf, text.replace(old, new))
    if check:
        print("all edits match")
        return
    for path, (crlf, text) in out.items():
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text.replace("\n", "\r\n") if crlf else text)
        print("patched", path)


main()