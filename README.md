# antigravity-cli-nix

Always up-to-date Nix package for the [Antigravity CLI](https://antigravity.google/) (`agy`) — Google's agentic coding CLI (the successor to Gemini CLI, powered by the former Windsurf/Codeium "Cascade" agent).

> **Beta**: This project is under active development by a solo maintainer and may break between updates. Use at your own risk. Contributions are welcome — feel free to open issues or submit pull requests!

## Why this package?

The official installer is a `curl … | install.cmd` (or `install.sh`) one-liner that downloads a prebuilt binary into `~/.local/bin` and edits your shell profile. That model does not fit NixOS: the binary hardcodes the glibc interpreter `/lib64/ld-linux-x86-64.so.2` (which does not exist on NixOS), and there is no nixpkgs entry. This flake lets you:

1. **Actually run it on NixOS** — `autoPatchelfHook` rewrites the interpreter to the Nix store loader
2. **Declarative installation** — managed in your NixOS or Home Manager config, instead of an imperative installer that mutates your shell profile
3. **Reproducible, pinned installs** — the exact binary is content-addressed by SHA512; no background self-update silently swaps it out

## Prebuilt binary, not built from source

Unlike the other flakes in this account, **antigravity-cli-nix does not build from source.** The Antigravity CLI is a closed-source Go binary built inside Google's `google3` monorepo — there is no public source tree and no GitHub repo, only a prebuilt-binary distribution channel. So this flake repackages the official binary the same way nixpkgs handles Slack/Discord/Zoom:

- `fetchurl` pulls the official upstream tarball for your platform (URL + SHA512 from the upstream auto-updater manifest, pinned in `sources.json`)
- `autoPatchelfHook` rewrites the hardcoded glibc interpreter to the Nix store loader

The binary links **only** against glibc (`libc`, `libm`, `libdl`, `libpthread`, `librt`, `libresolv`), so no extra dependencies are pulled in.

**Platform support:** `x86_64-linux` and `aarch64-linux` (glibc). The upstream channel advertises a musl variant but currently does not publish it (the manifest 404s), so **musl NixOS is unsupported** until Google ships it. `x86_64-darwin` / `aarch64-darwin` URLs are pinned in `sources.json` and the binary runs as-is on macOS, but the `autoPatchelfHook` step is Linux-only, so darwin is best-effort and untested.

## Project Structure

| File | Purpose |
|---|---|
| `flake.nix` | Flake definition: inputs (nixpkgs, flake-utils), overlay, packages, app; sets `config.allowUnfree = true` |
| `package.nix` | Repackage recipe: `stdenvNoCC` + `autoPatchelfHook` + `fetchurl`, per-platform source from `sources.json`, installs `agy` (+ `antigravity` alias) |
| `sources.json` | Per-platform `{ url, hash }` map + shared `version` |
| `default.nix` | Non-flake entry point (NUR-compatible), with `allowUnfree` |
| `flake.lock` | Pinned inputs |
| `update.sh` | Deterministic update workflow (reads the upstream manifest; no LLM, no nix-prefetch) |
| `.gitignore` | Excludes Nix build artifacts and editor files |

## Quick Start

The flake enables the unfree license internally, so these work without extra flags:

```bash
# Run directly without installing
nix run github:selfhost-it/antigravity-cli-nix

# Install to your profile
nix profile install github:selfhost-it/antigravity-cli-nix
```

The installed command is `agy` (with `antigravity` as an alias).

## NixOS / Home Manager Integration

### Add to your flake inputs

```nix
{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

    antigravity-cli = {
      url = "github:selfhost-it/antigravity-cli-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };
}
```

### Allow the unfree license

The Antigravity CLI is closed-source (unfree). Your own nixpkgs must permit it, either globally:

```nix
nixpkgs.config.allowUnfree = true;
```

or just for this package:

```nix
nixpkgs.config.allowUnfreePredicate = pkg:
  builtins.elem (lib.getName pkg) [ "antigravity-cli" ];
```

### Apply the overlay

```nix
{
  nixpkgs.overlays = [
    antigravity-cli.overlays.default
  ];
}
```

### Add to your packages

NixOS (`configuration.nix`):

```nix
environment.systemPackages = with pkgs; [
  antigravity-cli
];
```

Home Manager (`home.nix`):

```nix
home.packages = with pkgs; [
  antigravity-cli
];
```

## Building Locally

```bash
git clone git@github.com:selfhost-it/antigravity-cli-nix.git
cd antigravity-cli-nix
nix build .

# Test
./result/bin/agy --version

# Or run directly
nix run .
```

## Updating to a new Antigravity version

The upstream auto-updater manifest already publishes an exact `sha512` per platform, so updating needs **no `nix-prefetch` and no LLM** — `update.sh` is a plain deterministic script. Manual procedure:

1. Fetch each platform manifest:
   ```bash
   curl -fsSL https://antigravity-cli-auto-updater-974169037036.us-central1.run.app/manifests/linux_amd64.json
   # also: linux_arm64, darwin_amd64, darwin_arm64
   ```
   Each returns `{ version, url, sha512 }`.

2. Convert each manifest `sha512` (hex) to a Nix SRI hash:
   ```bash
   nix hash convert --hash-algo sha512 --to sri <HEX>
   ```

3. Write the new `version` and per-platform `url` / `hash` into `sources.json`.

4. Run `nix build .` and verify:
   ```bash
   ./result/bin/agy --version   # must report the new version
   ```

5. Commit and push.

The deterministic workflow `./update.sh` performs all of these steps (it auto-bootstraps into the devShell for `jq` / `curl`).

## Technical Details

- **Source**: closed-source prebuilt binary; no public source. Distribution channel reverse-engineered from the official installer at `https://antigravity.google/cli/install.sh`:
  - **manifest** (Cloud Run): `https://antigravity-cli-auto-updater-974169037036.us-central1.run.app/manifests/<platform>.json` → `{ version, url, sha512 }`
  - **binaries** (public GCS bucket): `https://storage.googleapis.com/antigravity-public/…`
- **Builder**: `stdenvNoCC.mkDerivation` (no compiler) + `autoPatchelfHook`; `dontBuild` / `dontStrip`
- **Runtime**: dynamically linked Go binary; glibc only (no `libstdc++`, no bundled Node/Electron library)
- **Binary**: `agy` (at `$out/bin/agy`), with an `antigravity` symlink matching the archive's member name
- **Interpreter fix**: upstream hardcodes `/lib64/ld-linux-x86-64.so.2`; `autoPatchelfHook` rewrites it to the Nix store glibc loader
- **Self-update**: the binary normally polls the manifest and overwrites itself in the background. In the read-only Nix store that write fails soft — which pins the version to this flake (bump via `update.sh`)
- **Skipped handoff**: the upstream installer also runs `agy install`, which edits shell profiles to set `PATH` and aliases. This package skips that step — on NixOS the package's `bin/` on `PATH` is the Nix way

## License

The Antigravity CLI is **unfree**: a closed-source binary distributed by Google under the [Antigravity terms of service](https://antigravity.google/). This repository only packages the official binary for Nix; it does not redistribute or relicense it.

---

Maintained by [self-host.it](https://self-host.it)
