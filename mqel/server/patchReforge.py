r"""Reforge patch v4. Put this file inside mqel\server (or next to the mqel folder) and run:
    python patchReforge.py
Works on UNPATCHED files and on files already patched by v1, v2 or v3.

Behaviour
  * Reforge keeps the quality and rolls a NEW item for the hero you are playing.
  * The slot is chosen at random among Weapon, Helm, Shoulders, Chest, Gloves, Cape and is
    always a DIFFERENT slot than the item you put in. Necklace and ring never come out.
  * Reforging your worn weapon can give armor. Only a NEW WEAPON is ever put into the main hand
    (_settle_forged_weapon): armor stays in your bag, a helm can no longer end up in MainHand.
  * Pets still come back as pets.
  * Every reforge writes  [reforge] hero=.. src_type=.. -> out_type=..  to the server log, and
    the server prints  [reforge] patch v4 active ...  when it starts.
  * Stamps REFORGE_PATCH_V4 into item_drops.py and account_state.py (verify with findstr).
  * Ruff-clean (no SIM115 / FURB167 / BLE001 warnings).

Then fully RESTART the server. Backups: .bak next to each file (never overwritten).
"""
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = next((r for r in (os.path.dirname(HERE), HERE, os.getcwd(), os.path.dirname(os.getcwd()),
                         os.path.join(os.getcwd(), "mqel"))
             if os.path.exists(os.path.join(r, "server", "item_drops.py"))), None)
if ROOT is None:
    sys.exit("Cannot find the mqel folder (server\\item_drops.py not found).")
DROPS = os.path.join(ROOT, "server", "item_drops.py")
STATE = os.path.join(ROOT, "server", "account_state.py")


def read(p):
    with open(p, "rb") as f:
        raw = f.read().decode("utf-8")
    return raw.replace("\r\n", "\n"), "\r\n" in raw


def write(p, text, crlf):
    if crlf:
        text = text.replace("\n", "\r\n")
    if not os.path.exists(p + ".bak"):
        shutil.copy(p, p + ".bak")
    with open(p, "wb") as f:
        f.write(text.encode("utf-8"))


NEW_REFORGE = '''# REFORGE_PATCH_V4
print("[reforge] patch v4 active, item_drops loaded from " + os.path.abspath(__file__), flush=True)

REFORGE_SLOTS = ("Weapon", "Helm", "ShoulderArmor", "SuitOfArmor", "Gloves", "Capes")


def reforge_item(source, level, quality, hero=None, keep_slot=False):
    """Umschmieden: gleiche Qualitaet, NEUES Item fuer den gespielten Helden `hero`,
    Slot zufaellig (Waffe/Helm/Schultern/Brust/Handschuhe/Umhang), nie derselbe Slot wie
    `source`; Ringe und Amulette kommen nie heraus. `keep_slot` wird ignoriert.
    Pets bleiben Pets. Ohne Held oder wenn nichts passt: gleicher Typ wie `source`."""
    src_type = _templates_by_id()[source["TemplateId"]]["HeroItemTypeId"]
    src_cat = _category(src_type)
    own = hero_item_types(hero) if hero else ()
    if not own or src_cat == "Pet":
        return _reforge_same_type(source, level, quality, src_type)
    by_cat = {}
    for t, _w in own:
        if _category(t) in REFORGE_SLOTS:
            by_cat.setdefault(_category(t), []).append(t)
    cats = [c for c in REFORGE_SLOTS if c in by_cat and c != src_cat]
    random.shuffle(cats)
    fallback = None
    for cat in cats:
        for type_id in by_cat[cat]:
            for _ in range(TYPE_ROLLS):
                item = _roll_typed(None, level, level, quality=quality, type_id=type_id)
                if item and item["TemplateId"] != source["TemplateId"]:
                    return item
                fallback = fallback or item
    return fallback or _reforge_same_type(source, level, quality, src_type)


def _reforge_same_type(source, level, quality, type_id):
    """Altes Verhalten: gleicher Item-Typ und Archetyp wie `source`."""
    item = _roll_typed(None, level, level, quality=quality, type_id=type_id)
    if item is None:  # Typen nur mit Named-Templates (z. B. Pets)
        named = [t for t in _templates() if t.get("Rarity") == "Named"
                 and _fits(t, type_id, level)]
        if named:
            t = random.choice(named)
            item = _item(t, level, [_effect(e["Id"], EFFECT_TYPE.get(e.get("Type"), 0),
                                            _effect_level(level))
                                    for e in t.get("Effects", [])])
    if item and source.get("ArchetypeId"):
        item["ArchetypeId"] = source["ArchetypeId"]
    return item'''

NEW_CALL = '''            if source is None:
                raise ValueError("Umschmieden ohne Ausgangsteil")
            # gleiche Qualitaet, gespielter Held, zufaelliger anderer Slot
            item = item_drops.reforge_item(source, level, quality, hero)
            if item is not None:  # REFORGE_PATCH_V4: Diagnose
                try:
                    _by = item_drops._templates_by_id()
                    _s = _by[source["TemplateId"]]["HeroItemTypeId"]
                    _o = _by[item["TemplateId"]]["HeroItemTypeId"]
                    self.log(f"    [reforge] hero={hero} src_type={_s}"
                             f"({item_drops._category(_s)}) -> out_type={_o}"
                             f"({item_drops._category(_o)}) worn={bool(worn)}")
                except Exception as e:  # noqa: BLE001
                    self.log(f"    [reforge] Diagnose fehlgeschlagen: {e!r}")'''

# the call block exactly as v3 wrote it
V3_CALL = (NEW_CALL.replace("REFORGE_PATCH_V4", "REFORGE_PATCH_V3")
           .replace("  # noqa: BLE001", ""))

OLD_CALL = "            item = item_drops.reforge_item(source, level, quality)"

# v1 / v2 call blocks (from the source check through the end of the diagnostic)
V12_CALL = re.compile(
    r'            if source is None:\n                raise ValueError\("Umschmieden ohne Ausgangsteil"\)\n'
    r'(?:            #[^\n]*\n)+'
    r'            item = item_drops\.reforge_item\(\n'
    r'                source, level, quality, hero,\n'
    r'                keep_slot=any\(w == "MainHand" for _, w in worn\)\)\n'
    r'(?:            if item is not None:  # REFORGE_PATCH_V2: Diagnose\n'
    r'(?:                [^\n]*\n)+?'
    r'                    self\.log\(f"    \[reforge\] Diagnose fehlgeschlagen: \{e!r\}"\)\n)?')

# _settle_forged_weapon: everything after `equipment = ...` up to the final log line
SETTLE_RE = re.compile(
    r'        equipment = self\._hero\(pending\["Hero"\]\)\["Equipment"\]\n'
    r'.*?'
    r'(?=        self\.log\("    \[state\] Schmiede: alte Waffe entfernt)',
    re.DOTALL)

NEW_SETTLE = '''        equipment = self._hero(pending["Hero"])["Equipment"]
        new_type = self.data.templates.get(
            pending["New"]["TemplateId"], {}).get("HeroItemTypeId")
        # REFORGE_PATCH_V4: nur eine Waffe kommt in die Hand, Ruestung bleibt in den Taschen
        new_is_weapon = item_drops._category(new_type) == "Weapon"
        if (equipment.get("MainHand") is None and new_is_weapon
                and self._remove_item(pending["New"])):
            equipment["MainHand"] = pending["New"]
        elif not self._remove_item(pending["Old"]):
            self.log("    [state] Schmiede: alte Waffe nicht gefunden")
        # ohne Waffe asserted der Client -> eine neue Waffe anlegen
        if (equipment.get("MainHand") is None and new_is_weapon
                and self._remove_item(pending["New"])):
            equipment["MainHand"] = pending["New"]
'''

drops, d_crlf = read(DROPS)
state, s_crlf = read(STATE)

if "REFORGE_PATCH_V4" in drops and "REFORGE_PATCH_V4" in state:
    sys.exit("v4 already applied in " + os.path.join(ROOT, "server") + ". If reforge is still "
             "wrong, the running server is not using these files: restart it fully and look "
             "for '[reforge] patch v4 active' at its start.")

# --- item_drops.py: replace reforge_item (+ marker, print, REFORGE_SLOTS, helper from v1-v3)
defs = list(re.finditer(r"^def reforge_item\(", drops, re.MULTILINE))
tail = "\n\n\ndef item_quality(item):"
if len(defs) != 1 or drops.count(tail) != 1:
    sys.exit(f"item_drops.py anchors not found (reforge_item x{len(defs)}, item_quality x"
             f"{drops.count(tail)}). Not patching.")
start, end = defs[0].start(), drops.index(tail)
if end < start:
    sys.exit("item_drops.py: unexpected order. Not patching.")
# an older patch put its marker (and print + REFORGE_SLOTS) in front of the def: take those too,
# but only if nothing else lives in between
marks = list(re.finditer(r"^# REFORGE_PATCH_V\d\n", drops, re.MULTILINE))
if marks and marks[0].start() < start:
    names = set(re.findall(r"^def (\w+)\(", drops[marks[0].start():end], re.MULTILINE))
    if names <= {"reforge_item", "_reforge_same_type"}:
        start = marks[0].start()
new_drops = drops[:start] + NEW_REFORGE + drops[end:]
if not re.search(r"^import os\b", new_drops, re.MULTILINE):
    sys.exit("item_drops.py has no 'import os'. Not patching.")

# --- account_state.py: original, v1, v2 or v3 reforge call -> v4 call
if state.count(NEW_CALL) == 1:
    new_state = state
elif state.count(V3_CALL) == 1:
    new_state = state.replace(V3_CALL, NEW_CALL)
elif state.count(OLD_CALL) == 1:
    new_state = state.replace(OLD_CALL, NEW_CALL)
else:
    found = V12_CALL.findall(state)
    if len(found) != 1:
        sys.exit("account_state.py: reforge call not found in original, v1, v2 or v3 form. "
                 "Not patching.")
    new_state = V12_CALL.sub(lambda _: NEW_CALL + "\n", state)

# --- account_state.py: _settle_forged_weapon only ever puts a WEAPON into the main hand
if len(SETTLE_RE.findall(new_state)) != 1 or "def _settle_forged_weapon(" not in new_state:
    sys.exit("account_state.py: _settle_forged_weapon not found in the expected form. "
             "Not patching.")
new_state = SETTLE_RE.sub(lambda _: NEW_SETTLE, new_state)

compile(new_drops, "item_drops.py", "exec")   # refuse to write broken Python
compile(new_state, "account_state.py", "exec")
write(DROPS, new_drops, d_crlf)
write(STATE, new_state, s_crlf)
print("Patched:\n  " + DROPS + "\n  " + STATE + "\nRESTART the server fully.")
print("Verify:  findstr REFORGE_PATCH_V4 server\\item_drops.py server\\account_state.py")