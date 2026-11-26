#!/usr/bin/env python3
"""setResolution.py - set the MQEL client's window/fullscreen size to ANY resolution.

Why this exists
  The in-game video options list is enumerated by Direct3D 9 from the player's monitor,
  so a config file cannot add entries to it. What the config *can* do is store the size
  the renderer starts with (WindowWidth / WindowHeight). This tool writes that value
  into both files that hold it, validated, with formatting and CRLF line endings untouched:
      Config/Game/CustomUserSettings.json   "WindowWidth":1280,
      Config/Game/EngineSettings.json       "Name":"WindowWidth", "Value":"1280"

IMPORTANT: close the game first. The engine rewrites both files on exit and would
overwrite the change. (On Windows the tool checks for MightyQuest.exe and refuses.)

Usage
  python server/setResolution.py --show
  python server/setResolution.py --list
  python server/setResolution.py 3440x1440
  python server/setResolution.py 3440x1440 --dry-run
  python server/setResolution.py 2560x1440 --config-dir "C:\\path\\to\\GameData\\Config\\Game"

No third-party packages needed (stdlib only).
"""
import argparse
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

MIN_W, MIN_H, MAX_W, MAX_H = 640, 480, 15360, 8640  # sanity bounds (8K x 2 wide)

HERE = Path(__file__).resolve().parent
DEFAULT_CFG = HERE.parent / "client" / "GameData" / "Config" / "Game"
CATALOGUE = "MonitorResolutions.json"

FILES = ("CustomUserSettings.json", "EngineSettings.json")


def _rx(file, axis):
    """Regex with 3 groups: prefix, number, suffix - for one Window<axis> entry."""
    if file == "CustomUserSettings.json":
        return re.compile(rf'("Window{axis}"\s*:\s*)(\d+)()')
    return re.compile(rf'("Name"\s*:\s*"Window{axis}"\s*,\s*"Value"\s*:\s*")(\d+)(")')


def game_running():
    if os.name != "nt":
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq MightyQuest.exe", "/NH"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "MightyQuest.exe" in out


def read_current(cfg):
    cur = {}
    for f in FILES:
        p = cfg / f
        if not p.exists():
            cur[f] = None
            continue
        txt = p.read_bytes().decode("utf-8")
        w = _rx(f, "Width").search(txt)
        h = _rx(f, "Height").search(txt)
        cur[f] = (int(w.group(2)), int(h.group(2))) if (w and h) else None
    return cur


def parse_res(s):
    m = re.fullmatch(r"\s*(\d{3,5})\s*[xX*,\s]\s*(\d{3,5})\s*", s)
    if not m:
        raise SystemExit(f"Resolution must look like 3440x1440 (got {s!r})")
    w, h = int(m.group(1)), int(m.group(2))
    if not (MIN_W <= w <= MAX_W and MIN_H <= h <= MAX_H):
        raise SystemExit(
            f"{w}x{h} is outside the sane range "
            f"{MIN_W}x{MIN_H} .. {MAX_W}x{MAX_H}"
        )
    return w, h


def load_catalogue(cfg):
    for p in (cfg / CATALOGUE, DEFAULT_CFG / CATALOGUE, HERE / CATALOGUE):
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    return None


def cmd_list(cfg):
    cat = load_catalogue(cfg)
    if not cat:
        raise SystemExit(f"{CATALOGUE} not found next to the config files.")
    cur = read_current(cfg).get("CustomUserSettings.json")
    order = ["16:9", "16:10", "ultrawide", "super-ultrawide", "high-res", "legacy"]
    for c in order:
        rows = [r for r in cat["resolutions"] if r["category"] == c]
        if not rows:
            continue
        print(f"\n== {c} ==")
        for r in rows:
            mark = "  <- current" if cur == (r["width"], r["height"]) else ""
            share = (
                f"  steam {r['steamShareAug2026Pct']:.2f}%"
                if "steamShareAug2026Pct" in r
                else ""
            )
            print(
                f"  {r['width']:>5}x{r['height']:<5} {r['name']:<16} "
                f"ui-zoom {r['uiZoom']:<5}{share}{mark}"
            )
    print("\nAny WxH is accepted by the tool, listed or not.")
    print("UI zoom is min(1, height/1080): the UI never scales up above 1080 lines tall.")


def apply(cfg, w, h, dry):
    plan = []
    for f in FILES:
        p = cfg / f
        if not p.exists():
            raise SystemExit(f"Missing {f} - is --config-dir right? ({cfg})")
        txt = p.read_bytes().decode("utf-8")
        new = txt
        for axis, val in (("Width", w), ("Height", h)):
            rx = _rx(f, axis)
            found = len(rx.findall(new))
            if found != 1:
                raise SystemExit(
                    f"{f}: expected exactly one Window{axis} entry, "
                    f"found {found} - refusing to guess."
                )
            new = rx.sub(lambda m, v=val: m.group(1) + str(v) + m.group(3), new)
        json.loads(new)  # must still be valid JSON
        plan.append((p, new.encode("utf-8"), txt != new))
    for p, _new, changed in plan:
        status = "will change" if changed else f"already {w}x{h}"
        print(f"{p.name:<28} {status}")
    if dry:
        print("(dry run - nothing written)")
        return
    if not any(changed for _, _, changed in plan):
        print("Nothing to do.")
        return
    for p, new, changed in plan:
        if not changed:
            continue
        bak = p.with_suffix(p.suffix + ".bak")
        if not bak.exists():
            shutil.copy2(p, bak)
        p.write_bytes(new)
    print(f"Done: {w}x{h}. Backups: *.json.bak (first run only).")
    if h >= 1080:
        print("Note: the UI renders at zoom 1.0 at this height (it does not scale up).")


def main():
    ap = argparse.ArgumentParser(
        description="Set the MQEL client resolution in its config files."
    )
    ap.add_argument("resolution", nargs="?", help="e.g. 3440x1440")
    ap.add_argument(
        "--config-dir",
        type=Path,
        default=DEFAULT_CFG,
        help="folder holding CustomUserSettings.json",
    )
    ap.add_argument("--list", action="store_true", help="show the monitor resolution catalogue")
    ap.add_argument("--show", action="store_true", help="show the resolution currently stored")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="skip the 'game is running' check")
    a = ap.parse_args()
    cfg = a.config_dir

    if a.list:
        return cmd_list(cfg)
    if a.show or not a.resolution:
        for f, v in read_current(cfg).items():
            print(f"{f:<28} {f'{v[0]}x{v[1]}' if v else 'not found'}")
        if not a.resolution:
            return None
    w, h = parse_res(a.resolution)
    if not a.dry_run and not a.force and game_running():
        raise SystemExit(
            "MightyQuest.exe is running - close it first "
            "(it overwrites these files on exit), or use --force."
        )
    return apply(cfg, w, h, a.dry_run)


if __name__ == "__main__":
    main()