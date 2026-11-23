"""Hero walk-over pickup respects the loot filter (MightyQuest_player.exe).

Problem
-------
The pet only collects gear at or above the quality chosen in Settings
("Loot pick-up by Pet", stored as AttackUserSettings.MinHeroItemQualityToPickUp).
The hero, however, picks up EVERYTHING he walks over, because the hero's
batch-pickup routine never looks at that setting. The filter lives only in the
pet's radius-query callback (VA 0xEB9B20).

What this patch does
--------------------
Hero auto-pickup funnels through LooterComponent::PickupList (VA 0xF0BDD0),
called from the looter tick (0xF0CB55) and from the pending-list flush
(0xF0C908). For every entity in that list we now skip it when

    loot type is 3 or 6 (gear)  AND  quality(item) < MinHeroItemQualityToPickUp

which is exactly the test the pet's callback performs. Skipped gear stays on
the ground. Gold, XP, life force, orbs, crafting materials are untouched.

Click-to-pickup and "loot all" call the single-entity routine 0xF0BB70
directly and never go through PickupList, so clicking still collects anything.
The pet is unchanged. "Pick-up everything" (min quality 0) behaves as before.

Mechanics
---------
* 9 bytes at VA 0xF0BE48 (two movs) become `jmp cave` + 4 nops.
* The cave (134 spare 0xCC bytes after the function, VA 0xF0BFF0) re-runs the
  two replaced movs, does the filter test, and either jumps back to 0xF0BE51
  or to the loop-continue label 0xF0BFB4 (= skip this entity).
* Only relative jumps/calls are used, so the PE relocation table is untouched.
* File size is unchanged (27,005,440 bytes), so the server's CRC/version
  handling (server/patcher.py computes crc32 of --client-exe) needs no edits.

Usage:  python tools/patch_loot.py <exe> [--out <new exe>] [--check]
Works on MightyQuest.exe (Steam) and on MightyQuest_player.exe (after
patch_ca.py); the touched bytes are identical in both. Re-running on an
already patched exe is a no-op.
"""

import argparse
import struct
import sys

IMAGE_BASE = 0x400000
TEXT_VA = 0x401000       # .text virtual address (with image base)
TEXT_RAW = 0x400         # .text raw file offset

HOOK_VA = 0xF0BE48       # first of the two movs inside PickupList's loop
HOOK_LEN = 9
RESUME_VA = HOOK_VA + HOOK_LEN      # 0xF0BE51
SKIP_VA = 0xF0BFB4       # loop-continue (next entity) in PickupList
QUALITY_FN_VA = 0xEDDF90  # int quality(itemSpec, gameData) - pure, cdecl
CAVE_VA = 0xF0BFF0
CAVE_MAX = 0xF0C070 - CAVE_VA       # 128 bytes of int3 padding

ORIG = bytes.fromhex("8b472c" "8b8d84000000")  # mov eax,[edi+2Ch]; mov ecx,[ebp+84h]


def off(va):
    return va - TEXT_VA + TEXT_RAW


def rel32(src_end_va, dst_va):
    return struct.pack("<i", dst_va - src_end_va)


def build_cave():
    c = bytearray()
    fix = []  # (position of rel8 byte, label)

    def emit(b):
        c.extend(bytes.fromhex(b))

    def jcc(op, label):
        c.append(op)
        fix.append((len(c), label))
        c.append(0)

    labels = {}

    emit("8b8d84000000")      # mov ecx,[ebp+84h]        ; loot spec
    emit("8b4114")            # mov eax,[ecx+14h]        ; loot type
    emit("83f803")            # cmp eax,3
    emit("7405")              # je  +5  (-> chk)
    emit("83f806")            # cmp eax,6
    jcc(0x75, "orig")         # jne orig                 ; not gear -> unchanged
    # chk:
    emit("8b8600020000")      # mov eax,[esi+200h]       ; loot entity -> game ctx
    emit("85c0")              # test eax,eax
    jcc(0x74, "orig")
    emit("8b80b0460000")      # mov eax,[eax+46B0h]
    emit("85c0")
    jcc(0x74, "orig")
    emit("8b8014010000")      # mov eax,[eax+114h]       ; AttackUserSettings*
    emit("85c0")
    jcc(0x74, "orig")
    emit("52")                # push edx                 ; owner (hero)
    emit("50")                # push eax                 ; settings
    emit("8b8200020000")      # mov eax,[edx+200h]
    emit("ffb0cc460000")      # push dword [eax+46CCh]   ; arg2: game data
    emit("ff7108")            # push dword [ecx+8]       ; arg1: item spec
    c.append(0xE8)            # call quality()
    c.extend(rel32(CAVE_VA + len(c) + 4, QUALITY_FN_VA))
    emit("83c408")            # add esp,8
    emit("59")                # pop ecx                  ; settings
    emit("5a")                # pop edx                  ; owner
    emit("3b4104")            # cmp eax,[ecx+4]          ; quality vs MinHeroItemQualityToPickUp
    jcc(0x7C, "skip")         # jl  skip
    labels["orig"] = len(c)
    c.extend(ORIG)            # the two instructions we displaced
    c.append(0xE9)
    c.extend(rel32(CAVE_VA + len(c) + 4, RESUME_VA))
    labels["skip"] = len(c)
    c.append(0xE9)
    c.extend(rel32(CAVE_VA + len(c) + 4, SKIP_VA))

    for pos, label in fix:
        d = labels[label] - (pos + 1)
        assert 0 <= d < 128, (label, d)
        c[pos] = d
    assert len(c) <= CAVE_MAX, len(c)
    return bytes(c)


def main():
    ap = argparse.ArgumentParser(description=(__doc__ or "patch_loot").splitlines()[0])
    ap.add_argument("exe")
    ap.add_argument("--out", help="target file (default: patch in place)")
    ap.add_argument("--check", action="store_true",
                    help="only report whether the exe is stock / patched / unknown")
    args = ap.parse_args()

    data = bytearray(open(args.exe, "rb").read())
    cave = build_cave()
    hook = b"\xE9" + rel32(HOOK_VA + 5, CAVE_VA) + b"\x90" * (HOOK_LEN - 5)

    cur_hook = bytes(data[off(HOOK_VA):off(HOOK_VA) + HOOK_LEN])
    cur_cave = bytes(data[off(CAVE_VA):off(CAVE_VA) + len(cave)])
    if cur_hook == hook and cur_cave == cave:
        print(f"{args.exe}: already patched")
        return
    if args.check:
        stock = cur_hook == ORIG and cur_cave == b"\xCC" * len(cave)
        print(f"{args.exe}: {'stock (unpatched)' if stock else 'UNKNOWN build - do not patch'}")
        return
    if cur_hook != ORIG:
        sys.exit(f"bytes at 0x{HOOK_VA:X} are {cur_hook.hex()}, expected {ORIG.hex()}: wrong exe?")
    if cur_cave != b"\xCC" * len(cave):
        sys.exit(f"code cave at 0x{CAVE_VA:X} is not free: wrong exe?")

    data[off(HOOK_VA):off(HOOK_VA) + HOOK_LEN] = hook
    data[off(CAVE_VA):off(CAVE_VA) + len(cave)] = cave
    out = args.out or args.exe
    with open(out, "wb") as f:
        f.write(data)
    print(f"{out}: hero walk-over pickup now honours the loot filter "
          f"(hook 0x{HOOK_VA:X}, cave 0x{CAVE_VA:X}, {len(cave)} bytes)")


if __name__ == "__main__":
    main()