#!/usr/bin/env python3
"""Update Antigravity from a consistent set of official platform manifests."""

import base64
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from update_common import Profile as BaseProfile, Target, UpdateError, Version, main


MANIFEST_BASE = "https://antigravity-cli-auto-updater-974169037036.us-central1.run.app"
PLATFORMS = {
    "x86_64-linux": "linux_amd64",
    "aarch64-linux": "linux_arm64",
    "x86_64-darwin": "darwin_amd64",
    "aarch64-darwin": "darwin_arm64",
}


def collect_sources(fetch_json):
    """Reject incomplete or staggered releases before creating a candidate."""
    platforms = {}
    release = None
    for system, platform in PLATFORMS.items():
        manifest = fetch_json(f"{MANIFEST_BASE}/manifests/{platform}.json")
        if not isinstance(manifest, dict):
            raise UpdateError(f"{platform}: manifest must be a JSON object")
        version, url, digest = (manifest.get(key) for key in ("version", "url", "sha512"))
        if not isinstance(version, str) or not re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", version):
            raise UpdateError(f"{platform}: missing or invalid stable version")
        if not isinstance(url, str):
            raise UpdateError(f"{platform}: missing download URL")
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.netloc != "storage.googleapis.com"
                or not parsed.path.startswith("/antigravity-public/")
                or parsed.query or parsed.fragment):
            raise UpdateError(f"{platform}: unexpected download URL")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{128}", digest):
            raise UpdateError(f"{platform}: missing or invalid SHA512 digest")
        if release is not None and version != release:
            raise UpdateError("Platform manifests advertise different versions; retry after the upstream rollout finishes")
        release = version
        platforms[system] = {
            "url": url,
            "hash": "sha512-" + base64.b64encode(bytes.fromhex(digest)).decode("ascii"),
        }
    return {"version": release, "platforms": platforms}


class AntigravityProfile(BaseProfile):
    name = "Antigravity CLI"
    files = ("sources.json",)
    binary = "agy"
    package_attr = "antigravity-cli"

    def current_version(self, root):
        sources = json.loads((Path(root) / "sources.json").read_text())
        return Version.parse(sources["version"])

    def discover(self, ctx, requested=None):
        sources = collect_sources(ctx.http_json)
        version = Version.parse(sources["version"])
        if requested is not None and requested != version:
            raise UpdateError("Antigravity manifests only provide the current release; the requested version is unavailable")
        return Target(version, sources)

    def is_current(self, root, target):
        return json.loads((Path(root) / "sources.json").read_text()) == target.payload

    def prepare(self, ctx, target):
        ctx.write_bytes("sources.json", (json.dumps(target.payload, indent=2) + "\n").encode())

    def version_matches(self, output, target):
        return output.strip() == str(target.version)

    def commit_subject(self, target):
        return f"Update Antigravity CLI to v{target.version}"


if __name__ == "__main__":
    raise SystemExit(main(AntigravityProfile()))
