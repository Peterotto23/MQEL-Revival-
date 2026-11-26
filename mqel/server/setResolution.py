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
import argparse, json, os, re, shutil, subprocess, sys
from pathlib import Path

MIN_W, MIN_H, MAX_W, MAX_H = 640, 480, 15360, 8640   # sanity bounds (8K x 2 wide)

HERE = Path(__file__).resolve().parent
DEFAULT_CFG = HERE.parent / "client" / "GameData" / "Config" / "Game"
CATALOGUE = "MonitorResolutions.json"

# (file, key, regex with 3 groups: prefix, number, suffix)
def _rx(file, axis):
    if file == "CustomUserSettings.json":
        return re.compile(r'("Window%s"\s*:\s*)(\d+)()' % axis)
    return re.compile(r'("Name"\s*:\s*"Window%s"\s*,\s*"Value"\s*:\s*")(\d+)(")' % axis)

FILES = ("CustomUserSettings.json", "EngineSettings.json")


def game_running():
    if os.name != "nt":
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq MightyQuest.exe", "/NH"],
                             capture_output=True, text=True, timeout=10).stdout
        return "MightyQuest.exe" in out
    except Exception:
        return False


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
        raise SystemExit("Resolution must look like 3440x1440 (got %r)" % s)
    w, h = int(m.group(1)), int(m.group(2))
    if not (MIN_W <= w <= MAX_W and MIN_H <= h <= MAX_H):
        raise SystemExit("%dx%d is outside the sane range %dx%d .. %dx%d"
                         % (w, h, MIN_W, MIN_H, MAX_W, MAX_H))
    return w, h


def _find_ci(folder, name):
    """Find `name` in `folder` ignoring capitalisation (Linux is case-sensitive, Windows is not)."""
    try:
        for p in folder.iterdir():
            if p.name.lower() == name.lower():
                return p
    except OSError:
        pass
    return None


def load_catalogue(cfg):
    folders = (cfg, HERE, HERE.parent / "client" / "GameData" / "Config" / "Game")
    for folder in folders:
        p = _find_ci(folder, CATALOGUE)
        if p:
            return json.loads(p.read_text(encoding="utf-8"))
    return None


def cmd_list(cfg):
    cat = load_catalogue(cfg)
    if not cat:
        raise SystemExit("%s not found next to the config files." % CATALOGUE)
    cur = read_current(cfg).get("CustomUserSettings.json")
    rows = sorted(cat["resolutions"], key=lambda r: (r["width"], r["height"]))
    print("%-11s %-24s %-7s %-16s %-7s %s" % ("size", "name", "aspect", "category", "ui-zoom", "notes"))
    for r in rows:
        notes = []
        if r.get("inDevEngineLog"):
            notes.append("in engine list")
        if "steamShareAug2026Pct" in r:
            notes.append("steam %.2f%%" % r["steamShareAug2026Pct"])
        if cur == (r["width"], r["height"]):
            notes.append("<- current")
        print("%-11s %-24s %-7s %-16s %-7s %s" % ("%dx%d" % (r["width"], r["height"]), r["name"], r["aspect"],
                                                 r["category"], r["uiZoom"], ", ".join(notes)))
    print("\nAny WxH is accepted by the tool, listed or not.")
    print("UI zoom is min(1, height/1080): the UI never scales up above 1080 lines tall.")


def apply(cfg, w, h, dry):
    plan = []
    for f in FILES:
        p = cfg / f
        if not p.exists():
            raise SystemExit("Missing %s - is --config-dir right? (%s)" % (f, cfg))
        raw = p.read_bytes()
        txt = raw.decode("utf-8")
        new = txt
        for axis, val in (("Width", w), ("Height", h)):
            rx = _rx(f, axis)
            if len(rx.findall(new)) != 1:
                raise SystemExit("%s: expected exactly one Window%s entry, found %d - refusing to guess."
                                 % (f, axis, len(rx.findall(new))))
            new = rx.sub(lambda m, v=val: m.group(1) + str(v) + m.group(3), new)
        json.loads(new)  # must still be valid JSON
        plan.append((p, raw, new.encode("utf-8"), txt != new))
    for p, raw, new, changed in plan:
        print("%-28s %s" % (p.name, "will change" if changed else "already %dx%d" % (w, h)))
    if dry:
        print("(dry run - nothing written)")
        return
    if not any(c for _, _, _, c in plan):
        print("Nothing to do.")
        return
    for p, raw, new, changed in plan:
        if not changed:
            continue
        bak = p.with_suffix(p.suffix + ".bak")
        if not bak.exists():
            shutil.copy2(p, bak)
        p.write_bytes(new)
    print("Done: %dx%d. Backups: *.json.bak (first run only)." % (w, h))
    if h >= 1080:
        print("Note: the UI renders at zoom 1.0 at this height (it does not scale up).")


def main():
    ap = argparse.ArgumentParser(description="Set the MQEL client resolution in its config files.")
    ap.add_argument("resolution", nargs="?", help="e.g. 3440x1440")
    ap.add_argument("--config-dir", type=Path, default=DEFAULT_CFG, help="folder holding CustomUserSettings.json")
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
            print("%-28s %s" % (f, "%dx%d" % v if v else "not found"))
        if not a.resolution:
            return
    w, h = parse_res(a.resolution)
    if not a.dry_run and not a.force and game_running():
        raise SystemExit("MightyQuest.exe is running - close it first (it overwrites these files on exit), or use --force.")
    apply(cfg, w, h, a.dry_run)


if __name__ == "__main__":
    main()