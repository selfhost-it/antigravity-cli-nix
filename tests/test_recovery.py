"""Exercise the original failed-build/retry bug with real local Git repositories."""

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import update_common as engine

spec = importlib.util.spec_from_file_location("antigravity_recovery_profile", SCRIPTS / "update.py")
update = importlib.util.module_from_spec(spec)
spec.loader.exec_module(update)


class RecoveryTests(unittest.TestCase):
    def test_failed_build_keeps_original_version_and_retry_rebuilds(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root, remote = parent / "checkout", parent / "origin.git"
            root.mkdir()
            run = engine.run_command
            run(["git", "init", "--bare", str(remote)])
            run(["git", "init", "--initial-branch=main", str(root)])
            for key, value in (("user.name", "Updater Test"), ("user.email", "test@example.invalid"),
                               ("commit.gpgsign", "false"), ("core.hooksPath", str(parent / "no-hooks"))):
                run(["git", "config", key, value], cwd=root)
            original = json.dumps({"version": "1.0.0", "platforms": {}}) + "\n"
            (root / "sources.json").write_text(original)
            run(["git", "add", "sources.json"], cwd=root)
            run(["git", "commit", "-m", "Initial fixture"], cwd=root)
            run(["git", "remote", "add", "origin", str(remote)], cwd=root)
            run(["git", "push", "--set-upstream", "origin", "main"], cwd=root)
            initial_head = run(["git", "rev-parse", "HEAD"], cwd=root).stdout
            builds = []

            def commands(args, **kwargs):
                if args[0] == "curl":
                    manifest = {"version": "1.0.1", "sha512": "ab" * 64,
                                "url": "https://storage.googleapis.com/antigravity-public/fixture.tar.gz"}
                    return engine.CommandResult(args, 0, json.dumps(manifest), "")
                if args[:2] == ["nix", "build"]:
                    builds.append(Path(kwargs["cwd"]))
                    if len(builds) == 1:
                        raise engine.UpdateError("simulated build failure: no space left")
                    binary = Path(kwargs["cwd"]) / "result" / "bin" / "agy"
                    binary.parent.mkdir(parents=True)
                    binary.write_text("#!/bin/sh\necho 1.0.1\n")
                    binary.chmod(0o755)
                    return engine.CommandResult(args, 0, "", "")
                return run(args, **kwargs)

            runner = engine.Runner(update.AntigravityProfile(), root=root, command=commands)
            with patch.object(engine, "MIN_FREE_BYTES", 0):
                with self.assertRaisesRegex(engine.UpdateError, "simulated build failure"):
                    runner.run(["--no-push"])
                self.assertEqual((root / "sources.json").read_text(), original)
                self.assertEqual(run(["git", "rev-parse", "HEAD"], cwd=root).stdout, initial_head)
                self.assertEqual(run(["git", "status", "--porcelain"], cwd=root).stdout, "")
                self.assertEqual(runner.run(["--no-push"]), 0)

            self.assertEqual(len(builds), 2)
            self.assertTrue(all(build != root for build in builds))
            self.assertEqual(json.loads((root / "sources.json").read_text())["version"], "1.0.1")
            self.assertEqual(run(["git", "status", "--porcelain"], cwd=root).stdout, "")
            self.assertNotEqual(run(["git", "rev-parse", "HEAD"], cwd=root).stdout, initial_head)
            self.assertEqual(run(["git", "--git-dir", str(remote), "rev-parse", "main"]).stdout, initial_head)


if __name__ == "__main__":
    unittest.main()
