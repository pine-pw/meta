#!/usr/bin/env python3
"""Validate the Pine meta tree.

Checks everything the Pine launcher relies on when it consumes
https://pine-pw.github.io/meta/v1/:

  * root index.json shape: formatVersion 1, packages with 64-hex sha256
  * every package has a <uid>/index.json whose bytes hash to the recorded sha256
  * version list entries hash-match their <uid>/<version>.json files
  * version files satisfy the OneSix/Meta parsers the launcher runs:
      - formatVersion 1, uid, version, releaseTime in Qt/ISO format
      - mainJar carries downloads.artifact {url, sha1, size} when present
      - libraries have valid "name" coordinates
      - minecraftArguments present for versions that ship game arguments
      - requires/conflicts only reference packages that exist
  * net.minecraft index is sorted newest-first with unique ids

Exit code 0 = clean, 1 = validation errors found.
"""

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime

HEX64 = re.compile(r"^[0-9a-f]{64}$")
HEX40 = re.compile(r"^[0-9a-f]{40}$")
COORDS = re.compile(r"^[^:\s]+:[^:\s]+:[^:\s]+(:[^:\s]+)?$")
ISO_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:\d{2}|Z)$")

ERRORS = []
WARNINGS = []


def err(path, msg):
    ERRORS.append(f"{path}: {msg}")


def warn(path, msg):
    WARNINGS.append(f"{path}: {msg}")


def load_json(path):
    try:
        with open(path, "rb") as fh:
            return json.loads(fh.read().decode("utf-8"))
    except (OSError, ValueError) as exc:
        err(path, f"unreadable/invalid JSON: {exc}")
        return None


def sha256_file(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def check_version_file(uid, vid, path):
    d = load_json(path)
    if d is None:
        return None
    where = f"{uid}/{vid}"
    if d.get("formatVersion") != 1:
        err(where, "formatVersion must be 1")
    if d.get("uid") != uid:
        err(where, f"uid mismatch: {d.get('uid')!r}")
    if d.get("version") != vid:
        err(where, f"version mismatch: {d.get('version')!r}")
    rt = d.get("releaseTime")
    if not isinstance(rt, str) or not ISO_TIME.match(rt):
        err(where, f"releaseTime not ISO: {rt!r}")
    else:
        try:
            datetime.fromisoformat(rt.replace("Z", "+00:00"))
        except ValueError:
            err(where, f"releaseTime unparseable: {rt!r}")
    if not isinstance(d.get("type"), str) or not d.get("type"):
        warn(where, "missing type")
    mj = d.get("mainJar")
    if mj is not None:
        art = (mj.get("downloads") or {}).get("artifact") or {}
        for key in ("url", "sha1", "size"):
            if key not in art:
                err(where, f"mainJar artifact missing {key}")
        if "sha1" in art and not HEX40.match(str(art["sha1"])):
            err(where, "mainJar artifact sha1 not 40-hex")
    libs = d.get("libraries", [])
    if not isinstance(libs, list):
        err(where, "libraries must be a list")
        libs = []
    for lib in libs:
        name = lib.get("name", "")
        if not COORDS.match(name):
            err(where, f"bad library coordinates: {name!r}")
    if uid == "net.minecraft":
        args = d.get("minecraftArguments")
        if not args:
            has_jar = mj is not None
            if has_jar:
                warn(where, "no minecraftArguments (ancient version?)")
        elif "${" in args and "${auth_player_name}" not in args:
            warn(where, "suspicious unresolved placeholders in minecraftArguments")
    for key in ("tweakers", "-libraries", "-tweakers", "-minecraftArguments", "+minecraftArguments"):
        if key in d:
            err(where, f"unsupported element {key!r} (launcher rejects it)")
    return d


def check_package(out, uid, name, expect_volatile):
    pdir = os.path.join(out, uid)
    index_path = os.path.join(pdir, "index.json")
    if not os.path.isfile(index_path):
        err(uid, "missing index.json")
        return None
    idx = load_json(index_path)
    if idx is None:
        return None
    if idx.get("formatVersion") != 1:
        err(f"{uid}/index.json", "formatVersion must be 1")
    if idx.get("uid") != uid:
        err(f"{uid}/index.json", f"uid mismatch: {idx.get('uid')!r}")
    versions = idx.get("versions")
    if not isinstance(versions, list) or not versions:
        err(f"{uid}/index.json", "versions must be a non-empty list")
        return idx
    seen = set()
    prev_time = None
    for e in versions:
        vid = e.get("version")
        if not vid:
            err(f"{uid}/index.json", "entry without version")
            continue
        if vid in seen:
            err(f"{uid}/index.json", f"duplicate version {vid}")
        seen.add(vid)
        sha = e.get("sha256")
        if not isinstance(sha, str) or not HEX64.match(sha):
            err(f"{uid}/index.json", f"{vid}: sha256 not 64-hex")
        else:
            fpath = os.path.join(pdir, f"{vid}.json")
            if not os.path.isfile(fpath):
                err(f"{uid}/index.json", f"{vid}: file missing ({vid}.json)")
            elif sha256_file(fpath) != sha:
                err(f"{uid}/index.json", f"{vid}: sha256 mismatch")
        rt = e.get("releaseTime")
        if not isinstance(rt, str) or not ISO_TIME.match(rt):
            err(f"{uid}/index.json", f"{vid}: bad releaseTime {rt!r}")
        elif prev_time is not None and rt > prev_time:
            warn(f"{uid}/index.json", f"{vid}: not sorted newest-first")
        prev_time = rt
        if expect_volatile and not e.get("volatile"):
            warn(f"{uid}/index.json", f"{vid}: expected volatile=true")
        # the version file itself
        fpath = os.path.join(pdir, f"{vid}.json")
        if os.path.isfile(fpath):
            check_version_file(uid, vid, fpath)
    return idx


def main():
    ap = argparse.ArgumentParser(description="Validate the Pine meta tree")
    ap.add_argument("--out", default=None, help="meta root (default: <repo>/v1)")
    args = ap.parse_args()

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = args.out or os.path.join(repo_root, "v1")

    root = load_json(os.path.join(out, "index.json"))
    if root is None:
        print("fatal: cannot read root index.json", file=sys.stderr)
        return 1
    if root.get("formatVersion") != 1:
        err("index.json", "formatVersion must be 1")
    packages = root.get("packages")
    if not isinstance(packages, list) or not packages:
        err("index.json", "packages must be a non-empty list")
        print("fatal: no packages", file=sys.stderr)
        return 1

    uids = set()
    names = {"net.minecraft": "Minecraft", "org.lwjgl": "LWJGL 2", "org.lwjgl3": "LWJGL 3"}
    for p in packages:
        uid = p.get("uid")
        if not uid:
            err("index.json", "package without uid")
            continue
        uids.add(uid)
        sha = p.get("sha256")
        if not isinstance(sha, str) or not HEX64.match(sha):
            err("index.json", f"{uid}: sha256 not 64-hex")
        else:
            ipath = os.path.join(out, uid, "index.json")
            if not os.path.isfile(ipath):
                err("index.json", f"{uid}: index.json missing")
            elif sha256_file(ipath) != sha:
                err("index.json", f"{uid}: sha256 mismatch with {uid}/index.json")
        if uid == "net.minecraft":
            check_package(out, uid, names.get(uid, uid), expect_volatile=False)
        elif uid in ("org.lwjgl", "org.lwjgl3"):
            check_package(out, uid, names.get(uid, uid), expect_volatile=True)
        else:
            warn("index.json", f"unknown package {uid} (not checked)")

    # requires/conflicts must reference existing packages
    mc_index = load_json(os.path.join(out, "net.minecraft", "index.json"))
    if mc_index:
        for e in mc_index.get("versions", []):
            for req in e.get("requires", []) + e.get("conflicts", []):
                if req.get("uid") not in uids:
                    err(f"net.minecraft/{e.get('version')}", f"references unknown package {req.get('uid')!r}")

    total_files = sum(len(files) for _, _, files in os.walk(out))
    print(f"checked {len(uids)} package(s), {total_files} files under {out}")
    for w in WARNINGS:
        print(f"WARN  {w}")
    for e in ERRORS:
        print(f"ERROR {e}")
    print(f"{len(WARNINGS)} warning(s), {len(ERRORS)} error(s)")
    return 1 if ERRORS else 0


if __name__ == "__main__":
    sys.exit(main())
