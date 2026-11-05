# Pine Meta

Self-hosted version metadata for the [Pine launcher](https://github.com/pine-pw/Pine).

This repository is the launcher's source of truth for Minecraft version
metadata. It is generated from Mojang's public version manifest and served as
a static site by GitHub Pages at:

**https://pine-pw.github.io/meta**

The launcher resolves every metadata file relative to
`https://pine-pw.github.io/meta/v1/` (see `META_MIRRORS` in the launcher's
`CMakeLists.txt`). `https://raw.githubusercontent.com/pine-pw/meta/main/` is
configured as a per-file fallback mirror.

## Endpoints

| URL | Contents |
| --- | --- |
| `/v1/index.json` | Root package index: all packages with the SHA-256 of their version list |
| `/v1/net.minecraft/index.json` | Minecraft version list (id, type, releaseTime, requires, sha256) |
| `/v1/net.minecraft/<version>.json` | Full version file for one Minecraft version |
| `/v1/org.lwjgl/index.json` | LWJGL 2 package (legacy Minecraft, built-in patch) |
| `/v1/org.lwjgl/<version>.json` | One LWJGL 2 version |
| `/v1/org.lwjgl3/index.json` | LWJGL 3 package (modern Minecraft) |
| `/v1/org.lwjgl3/<version>.json` | One LWJGL 3 version |

Example:

```
curl -s https://pine-pw.github.io/meta/v1/index.json | python3 -m json.tool
```

## Layout

```
v1/
├── index.json                  root package index
├── net.minecraft/
│   ├── index.json              Minecraft version list
│   └── <version>.json          one file per Minecraft version (922 of them)
├── org.lwjgl/
│   ├── index.json              LWJGL 2 version list
│   └── <version>.json          e.g. 2.9.1 (the launcher's built-in legacy patch)
└── org.lwjgl3/
    ├── index.json              LWJGL 3 version list
    └── <version>.json          e.g. 3.4.3 (what current Minecraft versions use)
```

## Format

The format is the MultiMC "OneSix" metadata format (formatVersion 1), which
the Pine launcher parses in `launcher/meta/JsonFormat.cpp`. It is documented
here so the repo can be maintained independently.

### sha256 chain

Every object knows the hash of everything below it. On startup the launcher
re-reads a file only when its recorded hash changed:

```
index.json ── sha256 ──> net.minecraft/index.json ── sha256 ──> net.minecraft/1.21.4.json
```

All hashes are lowercase hex SHA-256 over the exact file bytes. Files are
canonical JSON (sorted keys, 2-space indent, trailing newline) so regeneration
is byte-for-byte reproducible.

### Root index (`v1/index.json`)

```json
{
  "formatVersion": 1,
  "packages": [
    { "uid": "net.minecraft", "name": "Minecraft", "sha256": "…" },
    { "uid": "org.lwjgl",     "name": "LWJGL 2",   "sha256": "…" },
    { "uid": "org.lwjgl3",    "name": "LWJGL 3",   "sha256": "…" }
  ]
}
```

### Version list (`v1/<uid>/index.json`)

```json
{
  "formatVersion": 1,
  "uid": "net.minecraft",
  "name": "Minecraft",
  "versions": [
    {
      "version": "26.3",
      "type": "release",
      "releaseTime": "2026-09-15T11:23:02+00:00",
      "recommended": true,
      "requires": [ { "uid": "org.lwjgl3", "suggests": "3.4.3" } ],
      "sha256": "…"
    }
  ]
}
```

`releaseTime` is an ISO-8601 timestamp (parsed as `Qt::ISODate`). The version
list is sorted newest-first; `recommended` marks the newest release. LWJGL
entries additionally carry `volatile: true` and `conflicts`.

### Version file (`v1/<uid>/<version>.json`)

Minecraft version files are a thin wrapper around Mojang's version JSON:

| Field | Meaning |
| --- | --- |
| `formatVersion` | always `1` |
| `uid`, `version`, `name` | package identity (`net.minecraft`, the version id, `Minecraft`) |
| `order` | merge order hint (`-2`) |
| `type`, `releaseTime` | release / snapshot and when it shipped |
| `mainClass` | the game's main class |
| `minecraftArguments` | game arguments. Modern `arguments.game` arrays are **flattened** into this string (feature-gated entries excluded) |
| `+jvmArgs` | platform-neutral JVM arguments from `arguments.jvm` (OS-gated and launcher-owned entries such as `-cp` / `-Djava.library.path` are omitted) |
| `+traits` | exported feature flags: `feature:<name>` for each feature used by argument rules, plus `FirstThreadOnMacOS` when the version needs `-XstartOnFirstThread` |
| `assetIndex`, `assets` | asset index metadata (objects still download from Mojang's resource CDN) |
| `mainJar` | the client jar: `com.mojang:minecraft:<version>:client` with `downloads.artifact {url, sha1, size}` |
| `libraries` | Mojang-format libraries, **minus** everything in the `org.lwjgl*` groups |
| `requires` | `org.lwjgl3` (modern) or `org.lwjgl` (legacy) with a `suggests` version |

LWJGL package files contain the stripped Mojang `org.lwjgl*` library entries
(including their `rules`, `natives` classifiers and `extract` exclusions) so
the launcher can resolve them as a component dependency.

Ancient versions (pre-`downloads` era, e.g. `rd-132211`, Classic, Alpha)
carry whatever Mojang's manifest still publishes; some cannot provide a
client jar URL and will show a profile problem if instantiated.

## Regeneration

```
python3 tools/generate_meta.py                 # everything (all versions)
python3 tools/generate_meta.py --limit 20      # quick smoke run
python3 tools/validate_meta.py                 # verify the sha256 chain + format
```

Requirements: Python 3.8+ (standard library only). The generator reads only
`https://piston-meta.mojang.com/mc/game/version_manifest_v2.json` and the
per-version JSONs it links to.

## CI

* `validate.yml` — runs `tools/validate_meta.py` on every push and pull request.
* `sync.yml` — daily at 03:17 UTC, regenerates the whole tree from Mojang and
  commits the result if anything changed.

## Enabling GitHub Pages

The site is plain static content at the repository root:

1. Repository **Settings → Pages**
2. Source: **Deploy from a branch**
3. Branch: `main`, folder: `/ (root)` → Save
4. The site goes live at `https://pine-pw.github.io/meta/` (a `.nojekyll` file
   is included, so no Jekyll processing happens)

Because `META_URL` in the launcher is `META_MIRRORS.first() + "v1/"`, the
`v1/` directory must exist at the repository root exactly as in this repo.

## License

Tooling in `tools/` is Apache-2.0 (see [LICENSE](LICENSE)). Generated
metadata in `v1/` is converted from Mojang Studios' publicly published
version manifest; game assets and libraries themselves remain on Mojang's and
Maven's CDNs and are subject to their respective terms.
