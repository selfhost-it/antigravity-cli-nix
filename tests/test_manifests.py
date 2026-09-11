"""Manifest validation is independent from Git, Nix, and network access."""

import base64
import copy
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("antigravity_update", SCRIPTS / "update.py")
update = importlib.util.module_from_spec(spec)
spec.loader.exec_module(update)


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.manifests = {
            platform: {
                "version": "1.2.1",
                "url": f"https://storage.googleapis.com/antigravity-public/1.2.1/{platform}.tar.gz",
                "sha512": "ab" * 64,
            }
            for platform in update.PLATFORMS.values()
        }

    def fetch(self, url):
        return self.manifests[url.rsplit("/", 1)[1].removesuffix(".json")]

    def test_consistent_release_converts_all_hashes(self):
        result = update.collect_sources(self.fetch)
        self.assertEqual(result["version"], "1.2.1")
        self.assertEqual(set(result["platforms"]), set(update.PLATFORMS))
        expected = "sha512-" + base64.b64encode(bytes.fromhex("ab" * 64)).decode()
        self.assertTrue(all(source["hash"] == expected for source in result["platforms"].values()))

    def test_staggered_release_is_rejected(self):
        self.manifests["darwin_arm64"]["version"] = "1.2.0"
        with self.assertRaisesRegex(update.UpdateError, "different versions"):
            update.collect_sources(self.fetch)

    def test_malformed_manifest_fields_are_rejected(self):
        invalid = {
            "version": [None, "null", "", "1.2.1-beta", 123],
            "url": [None, "http://storage.googleapis.com/antigravity-public/a", "https://example.com/a"],
            "sha512": [None, "null", "", "ab" * 63, "zz" * 64],
        }
        original = copy.deepcopy(self.manifests)
        for field, values in invalid.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    self.manifests = copy.deepcopy(original)
                    self.manifests["linux_amd64"][field] = value
                    with self.assertRaises(update.UpdateError):
                        update.collect_sources(self.fetch)

    def test_http_error_object_is_rejected(self):
        self.manifests["linux_amd64"] = {"message": "Service unavailable"}
        with self.assertRaises(update.UpdateError):
            update.collect_sources(self.fetch)

    def test_same_version_with_changed_artifact_needs_update(self):
        sources = update.collect_sources(self.fetch)
        target = update.Target(update.Version.parse("1.2.1"), sources)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sources.json").write_text(json.dumps(sources))
            profile = update.AntigravityProfile()
            self.assertTrue(profile.is_current(root, target))
            sources_on_disk = copy.deepcopy(sources)
            sources_on_disk["platforms"]["aarch64-linux"]["hash"] = "sha512-old"
            (root / "sources.json").write_text(json.dumps(sources_on_disk))
            self.assertFalse(profile.is_current(root, target))


if __name__ == "__main__":
    unittest.main()
