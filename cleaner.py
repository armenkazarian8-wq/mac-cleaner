#!/usr/bin/env python3
"""Mac Cleaner backend — scans for reclaimable storage and cleans it.

Run inside "Mac Cleaner.app" (started with --app), or standalone:
    python3 ~/MacCleaner/cleaner.py      → opens http://127.0.0.1:8765 in your browser

Regenerable junk (caches, logs, temp files) is deleted outright so the space is freed
immediately. Anything personal (apps, documents, backups) is moved to the Trash instead.
"""
import glob
import hashlib
import json
import os
import plistlib
import secrets
import shlex
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOME = os.path.expanduser("~")
LIB = os.path.join(HOME, "Library")
TRASH = os.path.join(HOME, ".Trash")
HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN = secrets.token_hex(16)
ICON_CACHE = os.path.join(LIB, "Caches", "MacCleaner", "icons")

MAX_ITEMS = 1000  # per category, largest first
USER_DIRS = ["Desktop", "Documents", "Downloads", "Movies", "Music", "Pictures"]
SKIP_DIRS = {"node_modules", ".git", "Library", ".Trash"}
SKIP_EXT = (".app", ".photoslibrary", ".musiclibrary", ".tvlibrary", ".imovielibrary", ".fcpbundle")
INSTALLER_EXT = (".dmg", ".pkg", ".mpkg", ".iso", ".xip")
PARTIAL_EXT = (".crdownload", ".part", ".partial", ".download")
DATALESS = 0x40000000  # iCloud file not downloaded locally; never read these
BROWSERS = {  # app-support dir → (display name, process name)
    "Google/Chrome": ("Google Chrome", "Google Chrome"),
    "BraveSoftware/Brave-Browser": ("Brave", "Brave Browser"),
    "Microsoft Edge": ("Microsoft Edge", "Microsoft Edge"),
    "Arc/User Data": ("Arc", "Arc"),
}
BROWSER_CACHE_DIRS = ["Code Cache", "GPUCache", "DawnGraphiteCache", "DawnWebGPUCache", "Service Worker/CacheStorage",
                      "Service Worker/ScriptCache"]

# Ops that delete outright (space freed now) vs. move to the Trash (recoverable).
PERMANENT_OPS = {"delete", "admin_delete", "sim_device", "sim_runtime", "tm_snapshot"}

_registry = {}  # module → {item id: dict(op, arg, size, quit)}
_lock = threading.Lock()


# ───────────────────────── helpers ─────────────────────────

def run(cmd, timeout=120):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def du_bytes(path):
    try:
        st = os.lstat(path)
        if not os.path.isdir(path) or os.path.islink(path):
            return st.st_blocks * 512 or st.st_size
        out = run(["du", "-skx", path], timeout=300).stdout
        return int(out.split()[0]) * 1024 if out else 0
    except Exception:
        return 0


def sizes_of(paths):
    paths = list(set(paths))
    with ThreadPoolExecutor(max_workers=8) as ex:
        return dict(zip(paths, ex.map(du_bytes, paths)))


def children(path):
    try:
        return [os.path.join(path, n) for n in os.listdir(path) if n not in (".DS_Store", ".localized")]
    except OSError:
        return []


def mdfind(query):
    out = run(["mdfind", "-onlyin", HOME, query], timeout=60).stdout
    return [p for p in out.splitlines() if p]


def tilde(p):
    return "~" + p[len(HOME):] if p.startswith(HOME) else p


def ago(ts):
    if not ts:
        return "unknown"
    days = int((time.time() - ts) // 86400)
    return "today" if days < 1 else f"{days} day{'s' if days != 1 else ''} ago"


def has_full_disk_access():
    for probe in (os.path.join(LIB, "Mail"), os.path.join(LIB, "Safari")):
        if os.path.exists(probe):
            try:
                os.listdir(probe)
                return True
            except PermissionError:
                return False
    return True


def item(path, op, name=None, size=None, note="", warn="", quit=None, arg=None, kids=None, icon=None):
    return {"id": path, "path": path, "op": op, "arg": arg if arg is not None else path,
            "name": name or os.path.basename(path.rstrip("/")) or path, "size": size, "note": note,
            "warn": warn, "quit": quit, "children": kids or [], "icon": icon}


def category(cid, title, desc, items, preselect=False, warn="", sized=True):
    """Fill in missing sizes, drop empties, sort, cap and total a category."""
    if sized:
        need = [i["path"] for i in items if i["size"] is None and os.path.exists(i["path"])]
        sz = sizes_of(need)
        for i in items:
            if i["size"] is None and i["path"] in sz:
                i["size"] = sz[i["path"]]
        items = [i for i in items if (i["size"] or 0) > 0 or i["op"] == "tm_snapshot"]
    items.sort(key=lambda i: -((i["size"] or 0) + sum(k["size"] or 0 for k in i["children"])))
    found = len(items)
    items = items[:MAX_ITEMS]
    if found > len(items):
        desc += f" Showing the largest {len(items)} of {found}."
    total = sum((i["size"] or 0) + sum(k["size"] or 0 for k in i["children"]) for i in items)
    return {"id": cid, "title": title, "desc": desc, "warn": warn, "items": items,
            "total": total, "preselect": preselect}


def register(module, cats):
    reg = {}
    for c in cats:
        for i in c["items"]:
            for x in [i] + i["children"]:
                reg[x["id"]] = {"op": x["op"], "arg": x["arg"], "size": x["size"] or 0, "quit": x["quit"]}
    with _lock:
        _registry[module] = reg


# ───────────────────────── System Junk ─────────────────────────

def walk_user_files():
    """Yield (path, stat) for files in the user's main folders, plus Safari .download folders."""
    stack = [os.path.join(HOME, d) for d in USER_DIRS]
    seen = 0
    while stack and seen < 500_000:
        d = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            seen += 1
            try:
                if e.is_symlink():
                    continue
                if e.is_dir():
                    if e.name.endswith(".download"):
                        yield e.path, e.stat(follow_symlinks=False)
                    elif e.name not in SKIP_DIRS and not e.name.lower().endswith(SKIP_EXT):
                        stack.append(e.path)
                elif e.is_file():
                    yield e.path, e.stat(follow_symlinks=False)
            except OSError:
                pass


def junk_reason(name):
    low = name.lower()
    if low == ".ds_store":
        return "Finder view settings (recreated automatically)"
    if low.endswith(INSTALLER_EXT):
        return "Installer — not needed once the app is installed"
    if low.endswith(PARTIAL_EXT):
        return "Incomplete download"
    if low.endswith(".tmp") or name.endswith("~") or name.startswith("~$"):
        return "Temporary / backup file"
    return None


def scan_junk(opts):
    cats = []
    cats.append(category("user_cache", "User cache files", "Temporary data apps keep to load faster. Rebuilt automatically.",
                         [item(p, "delete") for p in children(os.path.join(LIB, "Caches"))
                          if not p.endswith("/MacCleaner")], preselect=True))
    cats.append(category("sys_cache", "System cache files", "Caches kept by macOS and system services. Needs your password.",
                         [item(p, "admin_delete") for p in children("/Library/Caches")], preselect=True))
    cats.append(category("user_logs", "User log files", "Diagnostic logs and crash reports from your apps.",
                         [item(p, "delete") for p in children(os.path.join(LIB, "Logs"))], preselect=True))
    syslogs = [item(p, "admin_delete") for p in children("/Library/Logs")]
    syslogs += [item(p, "admin_delete", note="Archived system log") for p in children("/private/var/log")
                if p.endswith((".gz", ".bz2")) or p.rsplit(".", 1)[-1].isdigit()]
    cats.append(category("sys_logs", "System log files", "Old system logs and diagnostic reports. Needs your password.",
                         syslogs, preselect=True))

    tmp = os.environ.get("TMPDIR") or run(["getconf", "DARWIN_USER_TEMP_DIR"]).stdout.strip()
    cutoff = time.time() - 2 * 86400
    temps = []
    for p in children(tmp):
        try:
            if os.lstat(p).st_mtime < cutoff:
                temps.append(item(p, "delete", note="Untouched for 2+ days"))
        except OSError:
            pass
    cats.append(category("temp", "Temporary files", "Leftover temp files that apps forgot to remove.", temps, preselect=True))

    cats.append(category("saved_state", "Saved app states", "Window positions apps restore on relaunch. Safe to clear.",
                         [item(p, "delete") for p in children(os.path.join(LIB, "Saved Application State"))],
                         preselect=True))

    dev = [("Developer/Xcode/DerivedData", "Xcode build data"), ("Developer/Xcode/iOS DeviceSupport", "iOS debug symbols"),
           ("Developer/Xcode/watchOS DeviceSupport", "watchOS debug symbols"),
           ("Developer/CoreSimulator/Caches", "Simulator caches")]
    devitems = [item(os.path.join(LIB, p), "delete", name=n) for p, n in dev if os.path.exists(os.path.join(LIB, p))]
    for p, n in ((".npm/_cacache", "npm cache"), (".gradle/caches", "Gradle cache"), (".cache", "Tool caches (~/.cache)"),
                 (".cargo/registry/cache", "Cargo cache"), ("go/pkg/mod/cache", "Go module cache"),
                 ("Library/Caches/pip", None)):
        full = os.path.join(HOME, p)
        if n and os.path.exists(full):
            devitems.append(item(full, "delete", name=n))
    cats.append(category("dev", "Developer junk", "Build products and package caches. Rebuilt when needed.",
                         devitems, preselect=True))

    bitems = []
    for rel, (bname, proc) in BROWSERS.items():
        base = os.path.join(LIB, "Application Support", rel)
        if not os.path.isdir(base):
            continue
        profiles = [base] + [p for p in children(base) if os.path.isdir(p)]
        for prof in profiles:
            for sub in BROWSER_CACHE_DIRS + (["component_crx_cache", "GrShaderCache", "ShaderCache"] if prof == base else []):
                p = os.path.join(prof, sub)
                if os.path.isdir(p):
                    label = os.path.basename(prof) if prof != base else ""
                    bitems.append(item(p, "delete", name=f"{bname} — {sub}" + (f" ({label})" if label else ""),
                                       note=f"Quit {bname} first", quit=proc))
    cats.append(category("browser", "Browser caches", "Website code and graphics caches. Browsers rebuild them as you browse.",
                         bitems))

    junk_kb = int(opts.get("junk_kb", 0))
    junk = []
    for path, st in walk_user_files():
        why = junk_reason(os.path.basename(path))
        if why and st.st_size >= junk_kb * 1024:
            op = "trash" if path.lower().endswith(INSTALLER_EXT) else "delete"
            junk.append(item(path, op, size=st.st_blocks * 512 or st.st_size, note=why))
    cats.append(category("unneeded", "Unnecessary files", "Installers, incomplete downloads, temp files and .DS_Store files.",
                         junk))
    return cats


# ───────────────────────── System Data ─────────────────────────

def scan_system(opts):
    cats = []

    sims = []
    devdir = os.path.join(LIB, "Developer/CoreSimulator/Devices")
    try:
        data = json.loads(run(["xcrun", "simctl", "list", "devices", "-j"], timeout=60).stdout or "{}")
        for rt, devs in data.get("devices", {}).items():
            rtname = rt.split(".")[-1].replace("-", " ", 1).replace("-", ".")
            for d in devs:
                path = os.path.join(devdir, d["udid"])
                if not os.path.isdir(path):
                    continue
                booted = d.get("state") == "Booted"
                note = f"{rtname} · " + ("running — shut it down first" if booted else
                                         ("unavailable runtime" if not d.get("isAvailable", True) else "apps & data inside it"))
                sims.append(item(path, "sim_device", name=d["name"], note=note, arg=d["udid"],
                                 warn="running" if booted else ""))
    except Exception:
        pass
    cats.append(category("sims", "iPhone & iPad simulators", "Each simulator stores its own apps and data. Xcode recreates "
                         "default simulators when needed.", sims))

    rts = []
    try:
        data = json.loads(run(["xcrun", "simctl", "runtime", "list", "-j"], timeout=60).stdout or "{}")
        for rid, r in data.items():
            if not r.get("deletable", True):
                continue
            plat = r.get("platformIdentifier", "").split(".")[-1].replace("simulator", "").replace("iphone", "iOS") \
                .replace("watch", "watchOS").replace("appletv", "tvOS").replace("xr", "visionOS")
            last = r.get("lastUsedAt")
            note = f"Build {r.get('build', '?')} · last used " + (last[:10] if last else "never")
            rts.append(item("simruntime:" + rid, "sim_runtime", name=f"{plat} {r.get('version', '')} Simulator runtime",
                            size=int(r.get("sizeBytes", 0)), note=note, arg=rid))
    except Exception:
        pass
    cats.append(category("runtimes", "Simulator runtimes", "Downloadable iOS/watchOS system images for simulators. "
                         "Re-download any time from Xcode › Settings › Components.", rts, sized=False))

    docker = os.path.join(LIB, "Containers/com.docker.docker/Data/vms/0/data/Docker.raw")
    gone = not any(os.path.exists(d) for d in ("/Applications/Docker.app", os.path.join(HOME, "Applications/Docker.app")))
    ditems = [item(docker, "delete", name="Docker disk image",
                   note="Docker is no longer installed — this is leftover" if gone else "Holds all Docker images, containers and volumes",
                   warn="Deletes everything stored in Docker. Quit Docker first.", quit="com.docker.backend")] \
        if os.path.exists(docker) else []
    cats.append(category("docker", "Docker data", "Docker's virtual disk. It is recreated empty the next time Docker starts.",
                         ditems))

    backups = []
    for p in children(os.path.join(LIB, "Application Support/MobileSync/Backup")):
        name, note = "Device backup", ""
        try:
            with open(os.path.join(p, "Info.plist"), "rb") as f:
                info = plistlib.load(f)
            name = f"{info.get('Device Name', 'Device')} backup"
            when = info.get("Last Backup Date")
            note = f"{info.get('Product Name', '')} · backed up {when:%b %d, %Y}" if when else info.get("Product Name", "")
        except Exception:
            pass
        backups.append(item(p, "trash", name=name, note=note))
    cats.append(category("ios_backups", "iPhone & iPad backups", "Local device backups. Moved to the Trash.", backups))

    upd = []
    for p in glob.glob(os.path.join(LIB, "iTunes/*Software Updates/*")):
        upd.append(item(p, "delete", note="Downloaded device update"))
    cats.append(category("ios_updates", "iOS software updates", "Device update files already installed.", upd,
                         preselect=True))

    arch = [item(p, "trash", note="Xcode archive") for p in glob.glob(os.path.join(LIB, "Developer/Xcode/Archives/*/*"))]
    cats.append(category("archives", "Xcode archives", "App builds you archived for distribution. Moved to the Trash.", arch))

    inst = [item(p, "trash", note="macOS installer") for p in glob.glob("/Applications/Install macOS*.app")]
    cats.append(category("installers", "macOS installers", "Full macOS installer apps. Moved to the Trash.", inst))

    mail = [item(p, "delete") for p in children(os.path.join(LIB, "Containers/com.apple.mail/Data/Library/Mail Downloads"))]
    cats.append(category("mail", "Mail attachments", "Copies of attachments you opened in Mail. Originals stay in Mail.",
                         mail))

    snaps = []
    for line in run(["tmutil", "listlocalsnapshots", "/"]).stdout.splitlines():
        line = line.strip()
        if line.startswith("com.apple.TimeMachine."):
            date = line.split(".")[3]
            snaps.append(item("snapshot:" + date, "tm_snapshot", name=f"Time Machine snapshot {date[:10]}",
                              note="Local snapshot · size not reported by macOS", arg=date))
    cats.append(category("snapshots", "Time Machine local snapshots", "Hourly snapshots macOS keeps on your disk. "
                         "Removing them frees their hidden space. Needs your password.", snaps, sized=False))
    return cats


# ───────────────────────── Trash ─────────────────────────

def scan_trash(opts):
    items = [item(p, "delete") for p in children(TRASH)]
    for vol in children("/Volumes"):
        items += [item(p, "delete", note=f"On {os.path.basename(vol)}")
                  for p in children(os.path.join(vol, ".Trashes", str(os.getuid())))]
    return [category("trash", "Trash", "Everything in your Trash. Deleted permanently.", items, preselect=True)]


# ───────────────────────── Large & Old / Duplicates ─────────────────────────

def scan_large(opts):
    large_mb, days = int(opts.get("large_mb", 200)), int(opts.get("days", 90))
    big = []
    for p in mdfind(f"kMDItemFSSize > {large_mb * 1024 * 1024}"):
        if "/Library/" in p or "/.Trash/" in p or not os.path.isfile(p):
            continue
        try:
            st = os.stat(p)
        except OSError:
            continue
        big.append(item(p, "trash", size=st.st_size, note=f"{tilde(os.path.dirname(p))} · opened {ago(st.st_atime)}"))
    label = f"{large_mb // 1024} GB" if large_mb >= 1024 else f"{large_mb} MB"
    cutoff = time.time() - days * 86400
    old = []
    for p in children(os.path.join(HOME, "Downloads")):
        try:
            st = os.lstat(p)
            if max(st.st_mtime, st.st_atime) < cutoff:
                old.append(item(p, "trash", note=f"Last used {ago(max(st.st_mtime, st.st_atime))}"))
        except OSError:
            pass
    return [category("large", f"Large files (over {label})", "Big files in your folders. Moved to the Trash.", big),
            category("old", f"Old downloads (over {days} days)", "Downloads you haven't opened in a while.", old)]


def file_hash(path, limit=None):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20 if limit is None else limit)
            if not chunk:
                break
            h.update(chunk)
            if limit is not None:
                break
    return h.hexdigest()


def scan_dupes(opts):
    min_bytes = max(1, int(opts.get("min_kb", 1024)) * 1024)
    by_size = {}
    for path, st in walk_user_files():
        if st.st_size >= min_bytes and not st.st_flags & DATALESS and os.path.isfile(path) and not junk_reason(
                os.path.basename(path)):
            by_size.setdefault(st.st_size, []).append((path, st))
    items = []
    for group in by_size.values():
        if len(group) < 2:
            continue
        by_hash = {}
        for path, st in group:
            try:
                key = (file_hash(path, 65536), file_hash(path) if st.st_size > 65536 else "")
            except OSError:
                continue
            by_hash.setdefault(key, []).append((path, st))
        for same in by_hash.values():
            if len(same) > 1:
                same.sort(key=lambda x: (x[1].st_birthtime, len(x[0])))
                for path, st in same[1:]:
                    items.append(item(path, "trash", size=st.st_size, note="Copy of " + tilde(same[0][0])))
    return [category("dupes", "Duplicate files", "Identical extra copies — the oldest copy is kept and not listed. "
                     "Copies inside games or projects may still be needed.", items)]


# ───────────────────────── Uninstaller ─────────────────────────

def find_apps():
    apps = []
    for root in ("/Applications", os.path.join(HOME, "Applications")):
        for p in children(root):
            if p.endswith(".app"):
                apps.append(p)
            elif os.path.isdir(p) and not os.path.islink(p):
                apps += [q for q in children(p) if q.endswith(".app")]
    return apps


def bundle_plist(path):
    """Info.plist of a Mac app, or of the inner bundle for iPhone/iPad apps installed on the Mac."""
    for cand in [os.path.join(path, "Contents/Info.plist"), os.path.join(path, "WrappedBundle/Info.plist")] + \
            glob.glob(os.path.join(glob.escape(path), "Wrapper/*.app/Info.plist")):
        try:
            with open(cand, "rb") as f:
                return plistlib.load(f), os.path.dirname(cand)
        except Exception:
            pass
    return {}, path


def app_info(path):
    info, inner = bundle_plist(path)
    bid = info.get("CFBundleIdentifier", "")
    last = None
    out = run(["mdls", "-raw", "-nullMarker", "", "-name", "kMDItemLastUsedDate", path], timeout=20).stdout
    if out:
        try:
            last = datetime.strptime(out, "%Y-%m-%d %H:%M:%S %z").timestamp()
        except ValueError:
            pass
    try:  # Spotlight often lacks a last-used date; the executable's access time fills the gap.
        exe = info.get("CFBundleExecutable", "")
        exe_path = os.path.join(inner, "MacOS", exe) if inner.endswith("Contents") else os.path.join(inner, exe)
        atime = os.stat(exe_path).st_atime
        last = max(last or 0, atime)
    except Exception:
        pass
    return bid, info.get("CFBundleName") or os.path.basename(path)[:-4], last


def leftovers(bid, name):
    if not bid:
        return []
    pats = [f"Application Support/{bid}", f"Caches/{bid}", f"Preferences/{bid}.plist", f"Preferences/ByHost/{bid}.*.plist",
            f"Containers/{bid}", f"Group Containers/*.{bid}", f"Saved Application State/{bid}.savedState",
            f"HTTPStorages/{bid}", f"HTTPStorages/{bid}.binarycookies", f"WebKit/{bid}", f"Logs/{bid}",
            f"Cookies/{bid}.binarycookies", f"LaunchAgents/{bid}*.plist", f"Application Scripts/{bid}"]
    if len(name) >= 4:
        pats += [f"Application Support/{name}", f"Caches/{name}", f"Logs/{name}"]
    found = []
    for pat in pats:
        found += glob.glob(os.path.join(LIB, glob.escape(os.path.dirname(pat)), os.path.basename(pat)))
    return sorted(set(found))


def running_paths():
    return run(["ps", "-axo", "comm="]).stdout


def scan_apps(opts):
    app_days = int(opts.get("app_days", 90))
    apps = find_apps()
    with ThreadPoolExecutor(max_workers=8) as ex:
        infos = dict(zip(apps, ex.map(app_info, apps)))
    procs = running_paths()
    all_items, unused, known_ids = [], [], set()
    for p, (bid, name, last) in infos.items():
        if bid:
            known_ids.add(bid.lower())
        # Apple's apps are skipped: some are system-protected, and Xcode provides the Python this app runs on.
        if bid.startswith("com.apple."):
            continue
        is_running = p + "/Contents/MacOS/" in procs
        kids = [item(k, "trash", note="Leftover · " + tilde(os.path.dirname(k))) for k in leftovers(bid, name)]
        it = item(p, "trash", name=name, note=f"Last opened {ago(last)}" + (" · running — quit it first" if is_running else ""),
                  kids=kids, icon=p, quit=p + "/Contents/MacOS/" if is_running else None)
        all_items.append(it)
        if not last or time.time() - last >= app_days * 86400:
            unused.append(it)

    # Leftovers from apps that are no longer installed (sandbox containers named after bundle IDs).
    orphans, candidates = [], []
    for p in children(os.path.join(LIB, "Containers")) + children(os.path.join(LIB, "Saved Application State")):
        bid = os.path.basename(p).replace(".savedState", "")
        low = bid.lower()
        if low.startswith("com.apple.") or "." not in bid or "-" in bid[:8]:
            continue
        vendor = ".".join(low.split(".")[:2]) + "."
        if any(k == low or low.startswith(k + ".") or k.startswith(vendor) for k in known_ids):
            continue
        candidates.append((p, bid))
    # Last check: ask Spotlight whether any bundle anywhere still has this ID (covers helpers and extensions).
    with ThreadPoolExecutor(max_workers=8) as ex:
        present = list(ex.map(lambda c: bool(run(["mdfind", f"kMDItemCFBundleIdentifier == '{c[1]}'"], 20).stdout.strip()),
                              candidates))
    for (p, bid), here in zip(candidates, present):
        if not here:
            orphans.append(item(p, "trash", name=bid, note="Leftover from a removed app · " + tilde(os.path.dirname(p))))

    size_kids(all_items)
    cats = [category("unused", f"Unused apps (over {app_days} days)", "Apps you haven't opened recently. "
                     "Removing an app also removes its leftover files.", unused),
            category("all", "All applications", "Every app you installed (Apple's own apps are hidden).", all_items),
            category("orphans", "Leftovers", "Files left behind by apps you already deleted.", orphans)]
    return cats


def size_kids(items):
    paths = [i["path"] for i in items] + [k["path"] for i in items for k in i["children"]]
    sz = sizes_of(paths)
    for i in items:
        i["size"] = sz.get(i["path"], 0)
        for k in i["children"]:
            k["size"] = sz.get(k["path"], 0)
        i["children"] = [k for k in i["children"] if k["size"]]


def app_icon(app):
    try:
        info, inner = bundle_plist(app)
        name = info.get("CFBundleIconFile", "")
    except Exception:
        return None
    res = os.path.join(inner, "Resources") if inner.endswith("Contents") else inner
    src = os.path.join(res, name if name.endswith(".icns") else name + ".icns") if name else ""
    if not os.path.exists(src):  # iPhone/iPad apps ship PNG icons instead
        pngs = sorted(glob.glob(os.path.join(glob.escape(inner), "AppIcon*.png")), key=os.path.getsize)
        if not pngs:
            return None
        src = pngs[-1]
    os.makedirs(ICON_CACHE, exist_ok=True)
    dst = os.path.join(ICON_CACHE, hashlib.sha1(app.encode()).hexdigest() + ".png")
    if not os.path.exists(dst) or os.path.getmtime(dst) < os.path.getmtime(src):
        run(["sips", "-s", "format", "png", "-Z", "64", src, "--out", dst], timeout=20)
    return dst if os.path.exists(dst) else None


# ───────────────────────── Optimization (launch agents) ─────────────────────────

def scan_agents(opts):
    cats = []
    for cid, title, d, op in (("user_agents", "Your launch agents", os.path.join(LIB, "LaunchAgents"), "agent_user"),
                              ("sys_agents", "System launch agents", "/Library/LaunchAgents", "agent_admin"),
                              ("daemons", "Background daemons", "/Library/LaunchDaemons", "agent_admin")):
        items = []
        for p in children(d):
            if not p.endswith(".plist") or os.path.basename(p).startswith("com.apple."):
                continue
            label, prog = os.path.basename(p)[:-6], ""
            try:
                with open(p, "rb") as f:
                    pl = plistlib.load(f)
                label = pl.get("Label", label)
                prog = pl.get("Program") or (pl.get("ProgramArguments") or [""])[0]
            except Exception:
                pass
            missing = prog and prog.startswith("/") and not os.path.exists(prog)
            note = ("⚠ its program no longer exists — safe to remove" if missing else tilde(prog) or "")
            items.append(item(p, op, name=label, size=os.path.getsize(p), note=note,
                              warn="broken" if missing else ""))
        cats.append(category(cid, title, "Programs that start automatically in the background. Removing one stops it "
                             "from launching." + (" Needs your password." if op == "agent_admin" else ""), items))
    for c in cats:  # list broken ones first, then by name
        c["items"].sort(key=lambda i: (not i["warn"], i["name"].lower()))
    return cats


SCANNERS = {"junk": scan_junk, "system": scan_system, "trash": scan_trash, "large": scan_large,
            "dupes": scan_dupes, "apps": scan_apps, "agents": scan_agents}


def scan(module, opts):
    cats = SCANNERS[module](opts)
    register(module, cats)
    for c in cats:
        for i in c["items"]:
            for x in [i] + i["children"]:
                x.pop("arg", None)
                x["permanent"] = x["op"] in PERMANENT_OPS
    return {"categories": cats, "fda": has_full_disk_access()}


# ───────────────────────── Space Lens ─────────────────────────

LENS_KEEP = 500  # children kept per folder; the rest are summed into one "smaller items" row
_lens = {"tree": {}, "running": False, "files": 0, "folders": 0, "bytes": 0, "current": "", "root": None,
         "scope": None, "started": 0, "finished": 0}
LENS_SKIP = {"/System/Volumes", "/Volumes", "/dev", "/net", "/home", "/private/var/vm"}
# Opening these makes macOS pop up a privacy prompt (Photos, Contacts, Calendars), so without Full Disk Access
# Space Lens lists them without looking inside.
LENS_PRIVATE = {os.path.join(LIB, p) for p in ("Application Support/AddressBook", "Calendars", "Reminders",
                                               "Group Containers/group.com.apple.reminders")}


def _lens_scan(scope):
    """Walk the whole tree once, recording every folder's total and its biggest children."""
    import stat as st_mod
    root = "/" if scope == "disk" else HOME
    devs = {os.stat("/").st_dev}
    if os.path.exists("/System/Volumes/Data"):
        devs.add(os.stat("/System/Volumes/Data").st_dev)
    tree, seen_links = {}, set()
    private_ok = has_full_disk_access()
    L = _lens
    L.update(tree=tree, files=0, folders=0, bytes=0, current=root, root=root, scope=scope,
             started=time.time(), finished=0)

    def walk(path):
        L["folders"] += 1
        L["current"] = path
        total, kids = 0, []
        try:
            it = os.scandir(path)
        except OSError:
            tree[path] = (0, [], 0, 0, False)
            return 0
        with it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                mode = st.st_mode
                if st_mod.S_ISLNK(mode):
                    continue
                if st_mod.S_ISDIR(mode):
                    if e.path in LENS_SKIP or st.st_dev not in devs:
                        continue
                    if not private_ok and (e.name.endswith(".photoslibrary") or e.path in LENS_PRIVATE):
                        kids.append((e.name, -1, 0))  # size unknown
                        continue
                    size = walk(e.path)
                    kids.append((e.name, size, 1))
                else:
                    if st.st_nlink > 1:  # count hard-linked files once
                        key = (st.st_dev, st.st_ino)
                        if key in seen_links:
                            continue
                        seen_links.add(key)
                    size = st.st_blocks * 512
                    L["files"] += 1
                    L["bytes"] += size
                    kids.append((e.name, size, 0))
                total += size
        kids.sort(key=lambda k: -k[1])
        rest = kids[LENS_KEEP:]
        tree[path] = (total, kids[:LENS_KEEP], len(rest), sum(k[1] for k in rest), True)
        return total

    try:
        walk(root)
    finally:
        L["running"] = False
        L["finished"] = time.time()


def lens_start(scope):
    if _lens["running"]:
        return lens_status()
    _lens["running"] = True
    sys.setrecursionlimit(100000)
    threading.stack_size(256 * 1024 * 1024)  # deep folder trees need a big stack
    threading.Thread(target=_lens_scan, args=(scope,), daemon=True).start()
    threading.stack_size(0)
    return lens_status()


def lens_status():
    L = _lens
    return {"running": L["running"], "ready": bool(L["finished"]) and not L["running"], "files": L["files"],
            "folders": L["folders"], "bytes": L["bytes"], "current": tilde(L["current"]), "root": L["root"],
            "scope": L["scope"], "seconds": round((L["finished"] or time.time()) - L["started"], 1) if L["started"] else 0}


def lens_removable(path):
    # Only things inside your home folder can be removed here, never the top-level folders themselves.
    parent = os.path.dirname(path)
    return path.startswith(HOME + "/") and parent not in (HOME, LIB)


def lens(path):
    tree = _lens["tree"]
    path = path or _lens["root"]
    if path not in tree:
        return {"missing": True, "path": path}
    total, kids, hidden_n, hidden_size, readable = tree[path]
    rows, reg = [], {}
    for name, size, is_dir in kids:
        p = os.path.join(path, name)
        r = {"id": p, "path": p, "name": name, "size": size if size >= 0 else None, "dir": bool(is_dir),
             "removable": lens_removable(p) and size >= 0}
        if size < 0:
            r["note"] = "Protected by macOS — grant Full Disk Access to measure"
        rows.append(r)
        if r["removable"]:
            reg[p] = {"op": "trash", "arg": p, "size": size, "quit": None}
    with _lock:
        _registry.setdefault("lens", {}).update(reg)
    root = _lens["root"]
    return {"path": path, "root": root, "parent": os.path.dirname(path) if path != root else None, "rows": rows,
            "total": total, "more": {"count": hidden_n, "size": hidden_size}, "readable": readable}


def lens_forget(path, size):
    """Drop a removed item from the scanned tree and subtract its size from every parent folder."""
    tree = _lens["tree"]
    parent, name = os.path.dirname(path), os.path.basename(path)
    if parent not in tree:
        return
    total, kids, hn, hs, ok = tree[parent]
    real = next((k[1] for k in kids if k[0] == name), size)
    tree[parent] = (total - real, [k for k in kids if k[0] != name], hn, hs, ok)
    tree.pop(path, None)
    p = parent
    while p != _lens["root"] and p != "/":
        p = os.path.dirname(p)
        if p in tree:
            t, ks, a, b, ok = tree[p]
            child = os.path.basename(parent)
            tree[p] = (t - real, [(n, sz - real, d) if n == child else (n, sz, d) for n, sz, d in ks], a, b, ok)
        parent = p


# ───────────────────────── Cleaning ─────────────────────────

def move_to_trash(path):
    base = os.path.basename(path.rstrip("/"))
    dest = os.path.join(TRASH, base)
    if os.path.exists(dest):
        stem, ext = os.path.splitext(base)
        dest = os.path.join(TRASH, f"{stem} {time.strftime('%H.%M.%S')}{ext}")
    try:
        shutil.move(path, dest)
    except PermissionError:
        # Finder can trash admin-owned items (it asks for your password) and supports "Put Back".
        script = 'on run argv\ntell application "Finder" to delete (POSIX file (item 1 of argv) as alias)\nend run'
        res = run(["osascript", "-e", script, path], timeout=300)
        if res.returncode != 0:
            raise OSError(res.stderr.strip() or "Finder could not move it to the Trash")


def delete_now(path):
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    else:
        os.remove(path)


def run_admin(commands):
    """Run shell commands as root behind one password prompt. Returns per-command error (or None)."""
    if not commands:
        return []
    script = "; ".join(f"({c}) >/dev/null 2>&1 || echo FAIL{i}" for i, c in enumerate(commands))
    res = run(["osascript", "-e", "on run argv", "-e",
               "do shell script (item 1 of argv) with administrator privileges", "-e", "end run", script], timeout=900)
    if res.returncode != 0:
        msg = "Cancelled — password not entered" if "-128" in res.stderr else (res.stderr.strip() or "admin step failed")
        return [msg] * len(commands)
    failed = {int(x[4:]) for x in res.stdout.split() if x.startswith("FAIL")}
    return ["Blocked by macOS (protected item)" if i in failed else None for i in range(len(commands))]


def is_running(pattern):
    return run(["pgrep", "-f" if "/" in pattern or "." in pattern else "-x", pattern]).returncode == 0


def clean(ids):
    with _lock:
        merged = {}
        for reg in _registry.values():
            merged.update(reg)
    done, failed, admin = [], [], []
    freed = trashed = 0
    q = shlex.quote
    for i in ids:
        e = merged.get(i)
        if not e:
            failed.append({"id": i, "error": "Not in the last scan — scan again"})
            continue
        if e["quit"] and is_running(e["quit"]):
            failed.append({"id": i, "error": "The app is running — quit it and try again"})
            continue
        op, arg = e["op"], e["arg"]
        try:
            if op == "delete":
                delete_now(arg)
            elif op == "trash":
                move_to_trash(arg)
            elif op == "agent_user":
                run(["launchctl", "bootout", f"gui/{os.getuid()}", arg])
                move_to_trash(arg)
            elif op == "sim_device":
                res = run(["xcrun", "simctl", "delete", arg], timeout=300)
                if res.returncode:
                    raise OSError(res.stderr.strip() or "simctl failed")
            elif op == "sim_runtime":
                res = run(["xcrun", "simctl", "runtime", "delete", arg], timeout=300)
                if res.returncode:
                    raise OSError(res.stderr.strip() or "simctl failed")
            elif op == "admin_delete":
                admin.append((i, e, f"/bin/rm -rf {q(arg)}"))
                continue
            elif op == "tm_snapshot":
                admin.append((i, e, f"/usr/bin/tmutil deletelocalsnapshots {q(arg)}"))
                continue
            elif op == "agent_admin":
                dest = os.path.join(TRASH, os.path.basename(arg))
                admin.append((i, e, f"/bin/launchctl bootout system {q(arg)}; /bin/mv -f {q(arg)} {q(dest)}"))
                continue
            done.append(i)
            if op in PERMANENT_OPS:
                freed += e["size"]
            else:
                trashed += e["size"]
        except Exception as ex:
            failed.append({"id": i, "error": str(ex).split("] ")[-1]})
    for (i, e, _), err in zip(admin, run_admin([c for _, _, c in admin])):
        if err:
            failed.append({"id": i, "error": err})
        else:
            done.append(i)
            freed += e["size"]
    for i in done:
        if i.startswith("/"):
            lens_forget(i, merged[i]["size"])
    return {"done": done, "failed": failed, "freed": freed, "trashed": trashed}


# ───────────────────────── Maintenance ─────────────────────────

MAINT = {
    "ram": ("/usr/sbin/purge", True),
    "dns": ("/usr/bin/dscacheutil -flushcache; /usr/bin/killall -HUP mDNSResponder", True),
    "snapshots": ("/usr/bin/tmutil thinlocalsnapshots / 999999999999 4", True),
    "spotlight": ("/usr/bin/mdutil -E /", True),
    "launchservices": (["/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/"
                        "Support/lsregister", "-kill", "-r", "-domain", "local", "-domain", "system", "-domain", "user"],
                       False),
    "fonts": (["atsutil", "databases", "-removeUser"], False),
    "quicklook": (["qlmanage", "-r", "cache"], False),
}


def maintain(tasks):
    results, admin = {}, []
    for t in tasks:
        if t not in MAINT:
            continue
        cmd, needs_admin = MAINT[t]
        if needs_admin:
            admin.append((t, cmd))
        else:
            res = run(cmd, timeout=300)
            results[t] = None if res.returncode == 0 else (res.stderr.strip() or "failed")
    for (t, _), err in zip(admin, run_admin([c for _, c in admin])):
        results[t] = err
    return results


# ───────────────────────── HTTP ─────────────────────────

def disk_status():
    du = shutil.disk_usage("/System/Volumes/Data" if os.path.exists("/System/Volumes/Data") else "/")
    return {"total": du.total, "used": du.total - du.free, "free": du.free}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body)).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _ok(self):
        return self.headers.get("X-Token") == TOKEN

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/":
            with open(os.path.join(HERE, "ui.html"), encoding="utf-8") as f:
                self._send(200, f.read().replace("__TOKEN__", TOKEN), "text/html; charset=utf-8")
        elif u.path == "/icon" and q.get("t") == TOKEN:
            png = app_icon(q.get("p", "")) if q.get("p", "").endswith(".app") else None
            if png:
                with open(png, "rb") as f:
                    self._send(200, f.read(), "image/png")
            else:
                self._send(404, b"", "image/png")
        elif not self._ok():
            self._send(403, {"error": "forbidden"})
        elif u.path == "/api/status":
            self._send(200, {"disk": disk_status(), "fda": has_full_disk_access(), "home": HOME})
        elif u.path == "/api/scan" and q.get("module") in SCANNERS:
            self._send(200, scan(q.pop("module"), q))
        elif u.path == "/api/lens":
            self._send(200, lens(q.get("path")))
        elif u.path == "/api/lens/status":
            self._send(200, lens_status())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._ok():
            return self._send(403, {"error": "forbidden"})
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path == "/api/clean":
            r = clean(body.get("ids", []))
            r["disk"] = disk_status()
            self._send(200, r)
        elif self.path == "/api/maint":
            self._send(200, {"results": maintain(body.get("tasks", [])), "disk": disk_status()})
        elif self.path == "/api/lens/start":
            self._send(200, lens_start("disk" if body.get("scope") == "disk" else "home"))
        elif self.path == "/api/reveal":
            run(["open", "-R", body.get("path", "")])
            self._send(200, {})
        elif self.path == "/api/open-fda":
            run(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"])
            self._send(200, {})
        else:
            self._send(404, {"error": "not found"})


def main():
    app_mode = "--app" in sys.argv
    server = ThreadingHTTPServer(("127.0.0.1", 0 if app_mode else 8765), Handler)
    port = server.server_address[1]
    if app_mode:
        print(f"PORT {port}", flush=True)
        parent = os.getppid()

        def watchdog():  # exit if the app that started us goes away
            while os.getppid() == parent:
                time.sleep(2)
            os._exit(0)
        threading.Thread(target=watchdog, daemon=True).start()
    else:
        url = f"http://127.0.0.1:{port}/"
        print(f"Mac Cleaner running at {url}  (Ctrl+C to quit)")
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
