# Antigravity CLI (`agy`) - Google's agentic coding CLI
#
# UNLIKE the other flakes in this repo (gemini/opencode/codeburn/github), this
# package does NOT build from source: the CLI is a closed-source Go binary built
# inside Google's `google3` monorepo (internal codename `jetski/cortex`; the
# engine is the former Windsurf/Codeium "Cascade" agent). There is no public
# source tree and no GitHub repo, only a prebuilt-binary distribution channel.
#
# So we repackage the official prebuilt binary the same way nixpkgs handles
# Slack/Discord/Zoom: fetch the upstream tarball, then `autoPatchelfHook`
# rewrites the hardcoded glibc interpreter (/lib64/ld-linux-x86-64.so.2, which
# does not exist on NixOS) to the Nix store loader. The binary links ONLY
# against glibc (libc/m/dl/pthread/rt/resolv) — no libstdc++, node, or electron
# library — so autoPatchelf needs no extra buildInputs.
#
# Distribution channel (reverse-engineered from the official installer):
#   installer  https://antigravity.google/cli/install.sh   (.cmd on Windows)
#   manifest   https://antigravity-cli-auto-updater-974169037036.us-central1.run.app/manifests/<platform>.json
#              -> { version, url, sha512 }
#   binaries   https://storage.googleapis.com/antigravity-public/...
# The manifest already ships a sha512, so `update.sh` converts it straight to a
# Nix SRI hash — no nix-prefetch round-trip needed.
#
# Self-update: the binary normally self-updates in the background by polling the
# same manifest endpoint and overwriting itself. In the read-only Nix store that
# write fails soft, which is exactly what we want — the version is pinned to this
# flake and bumped via `update.sh`. We also intentionally SKIP the installer's
# `agy install` step (it edits shell profiles to set PATH/aliases); on NixOS the
# package's bin/ on PATH is the Nix way.
#
# To update: run `./update.sh` (or bump `version` + the per-platform url/hash in
# `sources.json` by hand from the manifest endpoint, then `nix build`).

{ lib
, stdenvNoCC
, fetchurl
, autoPatchelfHook
}:

let
  sources = builtins.fromJSON (builtins.readFile ./sources.json);
  inherit (sources) version;
  system = stdenvNoCC.hostPlatform.system;
  source =
    sources.platforms.${system}
      or (throw "antigravity-cli: unsupported system '${system}' (no upstream binary in sources.json)");
in
stdenvNoCC.mkDerivation {
  pname = "antigravity-cli";
  inherit version;

  src = fetchurl {
    inherit (source) url hash;
  };

  # Each upstream tarball contains a single top-level file named `antigravity`,
  # so there is no directory to descend into after unpacking.
  sourceRoot = ".";

  nativeBuildInputs = lib.optionals stdenvNoCC.hostPlatform.isLinux [
    autoPatchelfHook
  ];

  dontConfigure = true;
  dontBuild = true;

  # It's a prebuilt Go binary; stripping it serves no purpose and can disturb
  # the Go buildinfo / embedded sections. autoPatchelfHook still patches it.
  dontStrip = true;

  # Upstream names the binary `antigravity` inside the tarball but installs it as
  # `agy` (the command users type). Expose both: `agy` is canonical, with an
  # `antigravity` alias matching the archive name.
  installPhase = ''
    runHook preInstall
    install -Dm755 antigravity "$out/bin/agy"
    ln -s agy "$out/bin/antigravity"
    runHook postInstall
  '';

  meta = {
    description = "Google Antigravity agentic coding CLI (`agy`) — repackaged prebuilt binary";
    homepage = "https://antigravity.google/";
    license = lib.licenses.unfree;
    sourceProvenance = [ lib.sourceTypes.binaryNativeCode ];
    platforms = builtins.attrNames sources.platforms;
    mainProgram = "agy";
  };
}
