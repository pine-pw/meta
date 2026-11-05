#!/usr/bin/env python3
"""Pine meta generator.

Fetches the official Minecraft version manifest from Mojang's piston-meta
service and converts it into the Pine meta format (the OneSix/MultiMC style
metadata that the Pine launcher consumes from
https://pine-pw.github.io/meta/v1/).

What it produces (under --out, default: ../v1 relative to this file):

    v1/index.json                     root package index (uid -> sha256 of <uid>/index.json)
    v1/net.minecraft/index.json       Minecraft version list
    v1/net.minecraft/<version>.json   one OneSix version file per Minecraft version
    v1/org.lwjgl/index.json           LWJGL 2 package (legacy Minecraft)
    v1/org.lwjgl/<version>.json
    v1/org.lwjgl3/index.json          LWJGL 3 package (modern Minecraft)
    v1/org.lwjgl3/<version>.json

Design notes, matched to what the Pine launcher (MultiMC/OneSix code) parses:

  * Version files carry "formatVersion": 1, "uid", "version", "releaseTime"
    (Qt::ISODate), "type", "mainClass", "assetIndex", "mainJar", "libraries".
  * Modern Mojang "arguments.game" arrays are flattened into a legacy
    "minecraftArguments" string, because the launcher's profile merge only
    understands the string form. Feature-gated arguments (demo, custom
    resolution, quick play) are left out of the flattened string; the feature
    keys are exported as "+traits" ("feature:<name>") so the launcher can see
    what the version supports.
  * LWJGL libraries are stripped out of the Minecraft version files and live
    in the org.lwjgl (2.x) / org.lwjgl3 (3.x) packages instead; the Minecraft
    file declares them through "requires" with a "suggests" version, exactly
    like the metadata the launcher is designed around.
  * "arguments.jvm" is exported as "+jvmArgs", keeping only entries that are
    not OS/arch gated and not launcher-owned (-cp / -Djava.library.path are
    built by the launcher itself).
  * Every file is canonical JSON (sorted keys, 2-space indent) and its sha256
    is recorded in the parent index, which is how the launcher decides what
    to re-download.

Only upstream public metadata is read; no third-party meta is copied.
"""

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, OrderedDict
from datetime import datetime, timezone

UPSTREAM_MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
USER_AGENT = "Pine-meta-generator/1.0 (+https://github.com/pine-pw/meta)"

# Groups that belong to the LWJGL packages rather than the Minecraft file.
LWJGL2_GROUP = "org.lwjgl.lwjgl"
LWJGL3_GROUPS = ("org.lwjgl", "org.lwjglx")

# JVM arguments the launcher builds itself; never export them in +jvmArgs.
JVM_ARGS_SKIP_EXACT = {"-cp", "${classpath}"}
JVM_ARGS_SKIP_PREFIX = ("-Djava.library.path=",)

# Placeholder tokens the launcher substitutes in +jvmArgs.
JVM_KNOWN_TOKENS = (
    "${natives_directory}",
    "${launcher_name}",
    "${launcher_version}",
    "${library_directory}",
)


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def http_get(url, tries=4, timeout=45):
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as exc:
            last = exc
            if attempt < tries - 1:
                delay = 2 ** attempt
                time.sleep(delay)
    raise RuntimeError(f"failed to fetch {url}: {last}")


def http_get_json(url):
    return json.loads(http_get(url).decode("utf-8"))


def canonical_json(obj):
    return (json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def lib_group(name):
    """group:artifact:version[:classifier] -> group"""
    return name.split(":", 1)[0] if ":" in name else ""


def lib_version(name):
    parts = name.split(":")
    return parts[2] if len(parts) >= 3 else ""


def lib_core_artifact(name):
    """The lwjgl core artifact name for a given lwjgl library entry."""
    parts = name.split(":")
    return parts[1] if len(parts) >= 2 else ""


def is_os_gated(rules):
    """True if any rule constrains on os/arch (we generate platform-neutral metadata)."""
    for rule in rules or []:
        if any(key in rule for key in ("os", "arch")):
            return True
    return False


def rule_allowed(rules, features):
    """Evaluate Mojang rules with the given feature set (default features are off)."""
    allowed = True
    for rule in rules or []:
        if "os" in rule or "arch" in rule:
            # OS rules are evaluated at launch time by the launcher itself for
            # libraries; for arguments we generate platform-neutral data.
            continue
        rule_features = rule.get("features", {})
        if not all(bool(features.get(k, False)) == bool(v) for k, v in rule_features.items()):
            continue
        allowed = rule.get("action") == "allow"
    return allowed


def rules_features(rules):
    """Collect feature names referenced by rules (for '+traits' export)."""
    out = set()
    for rule in rules or []:
        for key in rule.get("features", {}):
            out.add(key)
    return out


def flatten_game_args(game_args):
    """Flatten Mojang 'arguments.game' into a legacy minecraftArguments string."""
    parts = []
    traits = set()
    for entry in game_args:
        if isinstance(entry, str):
            parts.append(entry)
            continue
        if not isinstance(entry, dict):
            continue
        rules = entry.get("rules", [])
        value = entry.get("value", [])
        if isinstance(value, str):
            value = [value]
        for feature in rules_features(rules):
            traits.add(f"feature:{feature}")
        if rule_allowed(rules, {}):
            parts.extend(v for v in value if isinstance(v, str))
    args = " ".join(p for p in parts if p)
    args = re.sub(r"\s+", " ", args).strip()
    return args, traits


def export_jvm_args(jvm_args):
    """Export the platform-neutral, launcher-relevant subset of arguments.jvm."""
    out = []
    for entry in jvm_args:
        if isinstance(entry, str):
            rules, value = [], [entry]
        elif isinstance(entry, dict):
            rules = entry.get("rules", [])
            value = entry.get("value", [])
            if isinstance(value, str):
                value = [value]
        else:
            continue
        if is_os_gated(rules):
            continue  # launcher handles OS-specific JVM flags itself
        if not rule_allowed(rules, {}):
            continue
        for v in value:
            if not isinstance(v, str) or not v:
                continue
            if v in JVM_ARGS_SKIP_EXACT or v.startswith(JVM_ARGS_SKIP_PREFIX):
                continue
            out.append(v)
    return out


def uses_start_on_first_thread(jvm_args):
    """Detect the macOS '-XstartOnFirstThread' flag (exported as a trait)."""
    for entry in jvm_args or []:
        value = entry if isinstance(entry, str) else entry.get("value", "") if isinstance(entry, dict) else ""
        if isinstance(value, str) and "-XstartOnFirstThread" in value:
            return True
        if isinstance(value, list) and any(isinstance(v, str) and "-XstartOnFirstThread" in v for v in value):
            return True
    return False


def split_lwjgl(libraries):
    """Split Mojang libraries into (mc_libraries, lwjgl2, lwjgl3).

    lwjgl2 / lwjgl3 are lists of (core_version, library_object).
    """
    mc_libs, lw2, lw3 = [], [], []
    for lib in libraries:
        name = lib.get("name", "")
        group = lib_group(name)
        if group == LWJGL2_GROUP:
            lw2.append((lib_version(name), lib))
        elif group in LWJGL3_GROUPS:
            lw3.append((lib_version(name), lib))
        else:
            mc_libs.append(lib)
    return mc_libs, lw2, lw3


def pick_core_version(pairs):
    """Pick the lwjgl core version an entry set suggests, deterministically.

    Prefers the core 'lwjgl' artifact, then entries without OS rules,
    then the most frequent version, then the first seen.
    """
    versions = [v for v, _ in pairs if v]
    if not versions:
        return ""
    core = [(v, lib) for v, lib in pairs if lib_core_artifact(lib.get("name", "")) == "lwjgl"]
    if core:
        return core[0][0]
    counter = Counter(versions)
    best = max(counter.items(), key=lambda kv: (kv[1], versions.index(kv[0])))
    return best[0]


def convert_version(entry, mojang):
    """Convert one Mojang version JSON into the Pine meta version file."""
    mc_id = entry["id"]
    out = OrderedDict()
    out["formatVersion"] = 1
    out["uid"] = "net.minecraft"
    out["version"] = mc_id
    out["name"] = "Minecraft"
    out["order"] = -2
    out["type"] = mojang.get("type", entry.get("type", ""))
    out["releaseTime"] = mojang.get("releaseTime", entry.get("releaseTime", ""))

    if "mainClass" in mojang:
        out["mainClass"] = mojang["mainClass"]
    if "processArguments" in mojang and "minecraftArguments" not in mojang:
        out["processArguments"] = mojang["processArguments"]
    if "minecraftArguments" in mojang:
        out["minecraftArguments"] = re.sub(r"\s+", " ", mojang["minecraftArguments"]).strip()
    if "assets" in mojang:
        out["assets"] = mojang["assets"]
    if "assetIndex" in mojang:
        out["assetIndex"] = mojang["assetIndex"]
    if "minimumLauncherVersion" in mojang:
        # keep it only when it is within what the launcher understands
        if mojang["minimumLauncherVersion"] <= 21:
            out["minimumLauncherVersion"] = mojang["minimumLauncherVersion"]

    mc_libs, lw2, lw3 = split_lwjgl(mojang.get("libraries", []))
    if mc_libs:
        out["libraries"] = mc_libs

    # mainJar from the Mojang client download
    client = (mojang.get("downloads") or {}).get("client")
    if client:
        out["mainJar"] = {
            "name": f"com.mojang:minecraft:{mc_id}:client",
            "downloads": {"artifact": client},
        }

    # modern launch arguments
    traits = set()
    arguments = mojang.get("arguments")
    if isinstance(arguments, dict):
        if "game" in arguments:
            flat, arg_traits = flatten_game_args(arguments["game"])
            if flat:
                out["minecraftArguments"] = flat
            traits |= arg_traits
        if "jvm" in arguments:
            jvm = export_jvm_args(arguments["jvm"])
            if jvm:
                out["+jvmArgs"] = jvm
            if uses_start_on_first_thread(arguments["jvm"]):
                traits.add("FirstThreadOnMacOS")

    # LWJGL lives in its own packages; point at it through requires.
    requires = []
    if lw3:
        core = pick_core_version(lw3)
        requires.append({"uid": "org.lwjgl3", "suggests": core})
    elif lw2:
        core = pick_core_version(lw2)
        requires.append({"uid": "org.lwjgl", "suggests": core})
    if requires:
        out["requires"] = requires

    if traits:
        out["+traits"] = sorted(traits)

    return out, lw2, lw3


def build_lwjgl_package(uid, name, core_version, pairs, release_time, conflicts):
    """Build an org.lwjgl / org.lwjgl3 package file from Mojang library entries."""
    libs = []
    seen = set()
    for _, lib in pairs:
        key = json.dumps(lib, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        libs.append(lib)
    out = OrderedDict()
    out["formatVersion"] = 1
    out["uid"] = uid
    out["version"] = core_version
    out["name"] = name
    out["order"] = -2
    out["type"] = "release"
    out["releaseTime"] = release_time
    out["volatile"] = True
    if conflicts:
        out["conflicts"] = conflicts
    out["libraries"] = libs
    return out


class MetaWriter:
    def __init__(self, out_dir):
        self.out = out_dir
        self.mc_dir = os.path.join(out_dir, "net.minecraft")
        self.lw2_dir = os.path.join(out_dir, "org.lwjgl")
        self.lw3_dir = os.path.join(out_dir, "org.lwjgl3")
        for d in (self.out, self.mc_dir, self.lw2_dir, self.lw3_dir):
            os.makedirs(d, exist_ok=True)
        self.mc_files = {}        # version -> file bytes
        self.mc_entries = {}      # version -> index entry dict
        self.lwjgl2_files = {}    # lw version -> bytes
        self.lwjgl3_files = {}
        self.lwjgl2_meta = {}     # lw version -> {releaseTime}
        self.lwjgl3_meta = {}

    def write(self, path, data):
        with open(path, "wb") as fh:
            fh.write(data)

    def add_minecraft(self, entry, converted, lw2, lw3):
        mc_id = entry["id"]
        data = canonical_json(converted)
        self.mc_files[mc_id] = data
        version_release = converted["releaseTime"]
        for core, pairs in lw2_pairs_by_core(lw2):
            if core not in self.lwjgl2_files:
                pkg = build_lwjgl_package(
                    "org.lwjgl", "LWJGL 2", core, pairs, version_release,
                    conflicts=[{"uid": "org.lwjgl3"}],
                )
                self.lwjgl2_files[core] = canonical_json(pkg)
                self.lwjgl2_meta[core] = version_release
        for core, pairs in lw2_pairs_by_core(lw3):
            if core not in self.lwjgl3_files:
                pkg = build_lwjgl_package(
                    "org.lwjgl3", "LWJGL 3", core, pairs, version_release,
                    conflicts=[{"uid": "org.lwjgl"}],
                )
                self.lwjgl3_files[core] = canonical_json(pkg)
                self.lwjgl3_meta[core] = version_release
        index_entry = OrderedDict()
        index_entry["version"] = mc_id
        index_entry["type"] = converted.get("type", "")
        index_entry["releaseTime"] = version_release
        index_entry["recommended"] = False
        if converted.get("requires"):
            index_entry["requires"] = converted["requires"]
        self.mc_entries[mc_id] = index_entry

    def flush(self):
        # net.minecraft version files + index
        for mc_id, data in self.mc_files.items():
            self.write(os.path.join(self.mc_dir, f"{sanitize(mc_id)}.json"), data)
        versions = []
        for mc_id, entry in self.mc_entries.items():
            e = dict(entry)
            e["sha256"] = sha256_hex(self.mc_files[mc_id])
            versions.append(e)
        versions.sort(key=lambda e: e["releaseTime"], reverse=True)
        # mark the newest release as recommended
        for e in versions:
            if e["type"] == "release":
                e["recommended"] = True
                break
        mc_index = OrderedDict()
        mc_index["formatVersion"] = 1
        mc_index["uid"] = "net.minecraft"
        mc_index["name"] = "Minecraft"
        mc_index["versions"] = versions
        mc_index_data = canonical_json(mc_index)
        self.write(os.path.join(self.mc_dir, "index.json"), mc_index_data)

        packages = []
        for uid, name, files, meta, ddir in (
            ("net.minecraft", "Minecraft", {"index.json": mc_index_data}, {}, self.mc_dir),
            ("org.lwjgl", "LWJGL 2", self.lwjgl2_files, self.lwjgl2_meta, self.lw2_dir),
            ("org.lwjgl3", "LWJGL 3", self.lwjgl3_files, self.lwjgl3_meta, self.lw3_dir),
        ):
            if uid != "net.minecraft":
                for core, data in files.items():
                    self.write(os.path.join(ddir, f"{sanitize(core)}.json"), data)
                versions_out = []
                for core, data in files.items():
                    versions_out.append({
                        "version": core,
                        "type": "release",
                        "releaseTime": meta.get(core, now_iso()),
                        "recommended": False,
                        "volatile": True,
                        "conflicts": [{"uid": "org.lwjgl3" if uid == "org.lwjgl" else "org.lwjgl"}],
                        "sha256": sha256_hex(data),
                    })
                versions_out.sort(key=lambda e: e["releaseTime"], reverse=True)
                pkg_index = OrderedDict()
                pkg_index["formatVersion"] = 1
                pkg_index["uid"] = uid
                pkg_index["name"] = name
                pkg_index["versions"] = versions_out
                data = canonical_json(pkg_index)
                self.write(os.path.join(ddir, "index.json"), data)
                file_bytes = data
            else:
                file_bytes = mc_index_data
            packages.append(OrderedDict([
                ("uid", uid),
                ("name", name),
                ("sha256", sha256_hex(file_bytes)),
            ]))

        root_index = OrderedDict()
        root_index["formatVersion"] = 1
        root_index["packages"] = sorted(packages, key=lambda p: p["uid"])
        self.write(os.path.join(self.out, "index.json"), canonical_json(root_index))

        return {
            "minecraft": len(self.mc_files),
            "lwjgl2": len(self.lwjgl2_files),
            "lwjgl3": len(self.lwjgl3_files),
        }


def lw2_pairs_by_core(pairs):
    grouped = OrderedDict()
    for core, lib in pairs:
        grouped.setdefault(core, []).append((core, lib))
    return [(core, plist) for core, plist in grouped.items()]


def sanitize(name):
    """Filesystem-safe file name.

    The launcher fetches '<uid>/<version>.json' using the raw version id, so
    the file name must keep spaces exactly as Mojang writes them (upstream
    meta serves e.g. '1.14.2 Pre-Release 4.json'; clients percent-encode the
    space in the URL). Only reject characters that would escape the directory.
    """
    if "/" in name or "\\" in name or "\x00" in name:
        raise ValueError(f"unsafe version id: {name!r}")
    return name


def fetch_version(entry):
    url = entry["url"]
    try:
        return entry, http_get_json(url), None
    except Exception as exc:  # noqa: BLE001 - report and continue
        return entry, None, str(exc)


def main():
    ap = argparse.ArgumentParser(description="Generate Pine meta from Mojang's manifest")
    ap.add_argument("--out", default=None, help="output directory (default: <repo>/v1)")
    ap.add_argument("--upstream", default=UPSTREAM_MANIFEST, help="upstream version manifest URL")
    ap.add_argument("--limit", type=int, default=0, help="only convert the N most recent versions (0 = all)")
    ap.add_argument("--versions", default="", help="comma-separated explicit version ids")
    ap.add_argument("--jobs", type=int, default=16, help="parallel downloads")
    args = ap.parse_args()

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = args.out or os.path.join(repo_root, "v1")

    log(f"fetching upstream manifest: {args.upstream}")
    manifest = http_get_json(args.upstream)
    all_entries = manifest.get("versions", [])
    if not all_entries:
        log("upstream manifest has no versions")
        return 1
    log(f"upstream reports {len(all_entries)} versions "
        f"(latest release: {manifest.get('latest', {}).get('release')}, "
        f"latest snapshot: {manifest.get('latest', {}).get('snapshot')})")

    if args.versions:
        wanted = [v.strip() for v in args.versions.split(",") if v.strip()]
        entries = [e for e in all_entries if e["id"] in wanted]
        missing = set(wanted) - {e["id"] for e in entries}
        if missing:
            log(f"warning: not found upstream: {', '.join(sorted(missing))}")
    else:
        entries = list(all_entries)
        if args.limit > 0:
            entries = entries[: args.limit]

    log(f"converting {len(entries)} versions with {args.jobs} workers ...")
    writer = MetaWriter(out_dir)
    failures = []
    results = {}
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {pool.submit(fetch_version, e): e for e in entries}
        for future in concurrent.futures.as_completed(futures):
            entry, mojang, error = future.result()
            done += 1
            if error is not None:
                failures.append((entry["id"], error))
                log(f"[{done}/{len(entries)}] FAILED {entry['id']}: {error}")
                continue
            try:
                results[entry["id"]] = convert_version(entry, mojang)
            except Exception as exc:  # noqa: BLE001
                failures.append((entry["id"], repr(exc)))
                log(f"[{done}/{len(entries)}] FAILED convert {entry['id']}: {exc!r}")
                continue
            if done % 50 == 0 or done == len(entries):
                log(f"[{done}/{len(entries)}] downloaded/converted")

    # Add in manifest order (newest first) so derived packages are deterministic.
    for entry in entries:
        if entry["id"] in results:
            converted, lw2, lw3 = results[entry["id"]]
            writer.add_minecraft(entry, converted, lw2, lw3)

    if failures:
        log(f"{len(failures)} version(s) failed; first few: "
            + "; ".join(f"{vid}: {err[:80]}" for vid, err in failures[:5]))

    stats = writer.flush()
    log(f"wrote meta to {out_dir}: "
        f"{stats['minecraft']} minecraft versions, "
        f"{stats['lwjgl2']} lwjgl2 packages, {stats['lwjgl3']} lwjgl3 packages")
    return 2 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
