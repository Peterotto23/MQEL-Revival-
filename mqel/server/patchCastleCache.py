r"""Patch: cache fake_player_castles() so real_player_castles() stops rebuilding
all ~49 Ersatzspieler castles from disk once per player.

Run from the mqel folder (the one that contains server\):
    python patch_castle_cache.py
A backup is written to server\castles.py.bak. Safe to run twice.
"""
import os
import shutil
import sys

path = os.path.join("server", "castles.py")
if not os.path.exists(path):
    sys.exit("Run this from the mqel folder (server\\castles.py not found).")

with open(path, "rb") as f:
    text = f.read().decode("utf-8")
nl = "\r\n" if "\r\n" in text else "\n"
anchor = "def fake_player_castles():"

if "_build_fake_player_castles" in text:
    sys.exit("Already patched, nothing to do.")
if text.count(anchor) != 1:
    sys.exit(f"Expected exactly one '{anchor}', found {text.count(anchor)}. Not patching.")

wrapper = nl.join([
    "import threading as _threading",
    "import time as _time",
    "",
    "# fake_player_castles() reads ~8500 castle JSON files. player_castle() calls it",
    "# for every real player without a published castle, so one attack selection",
    "# (More Castles / GetAttackSelectionList) rebuilt it once per player: ~4-5 s",
    "# with 174 accounts. Cache it; the TTL keeps time-limited event castles fresh.",
    "_FAKES_TTL = 300  # seconds",
    "_fakes_cache = (0.0, None)",
    "_fakes_lock = _threading.Lock()",
    "",
    "",
    "def fake_player_castles():",
    '    """Ersatzspieler (zwischengespeichert, siehe _build_fake_player_castles).',
    "    Liefert eine neue Liste, die Burg-Dicts sind geteilt: nicht veraendern,",
    '    vorher kopieren (player_castle macht das schon)."""',
    "    global _fakes_cache",
    "    stamp, value = _fakes_cache",
    "    if value is not None and _time.monotonic() - stamp < _FAKES_TTL:",
    "        return list(value)",
    "    with _fakes_lock:",
    "        stamp, value = _fakes_cache",
    "        if value is None or _time.monotonic() - stamp >= _FAKES_TTL:",
    "            value = _build_fake_player_castles()",
    "            _fakes_cache = (_time.monotonic(), value)",
    "        return list(value)",
    "",
    "",
    "",
])

shutil.copy(path, path + ".bak")
text = text.replace(anchor, wrapper + "def _build_fake_player_castles():")
with open(path, "wb") as f:
    f.write(text.encode("utf-8"))
print("Patched server\\castles.py (backup: castles.py.bak). Restart the server.")