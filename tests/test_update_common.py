from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import update_common as common  # noqa: E402


def shell(args, *, cwd=None, check=True, env=None, timeout=None):
    return common.run_command(args, cwd=cwd, check=check, env=env, timeout=timeout)


class DummyProfile(common.Profile):
    name = "Dummy"
    files = ("package.nix", "data.json")
    binary = "dummy"

    def __init__(self, reported="1.1.0", extra_path=None):
        self.reported = reported
        self.extra_path = extra_path

    def current_version(self, root):
        return common.package_version(root)

    def discover(self, ctx, requested):
        return common.Target(requested or common.Version.parse("1.1.0"))

    def prepare(self, ctx, target):
        common.set_package_version(ctx, target.version)
        ctx.write_bytes("data.json", b'{"updated": true}\n')
        if self.extra_path:
            ctx.write_bytes(self.extra_path, b"unexpected\n")


class GitFixture:
    def __init__(self):
        self.stack = contextlib.ExitStack()
        self.temp = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="updater-test-")))
        self.root = self.temp / "repo"
        self.remote = self.temp / "remote.git"
        shell(["git", "init", "--bare", str(self.remote)])
        shell(["git", "init", "-b", "main", str(self.root)])
        shell(["git", "config", "user.name", "Updater Test"], cwd=self.root)
        shell(["git", "config", "user.email", "updater@example.invalid"], cwd=self.root)
        (self.root / "package.nix").write_text('version = "1.0.0";\n')
        (self.root / "data.json").write_text('{"updated": false}\n')
        shell(["git", "add", "package.nix", "data.json"], cwd=self.root)
        shell(["git", "commit", "-m", "initial"], cwd=self.root)
        shell(["git", "remote", "add", "origin", str(self.remote)], cwd=self.root)
        shell(["git", "push", "-u", "origin", "main"], cwd=self.root)

    def close(self):
        self.stack.close()


class FakeBuildCommand:
    def __init__(self, reported="1.1.0", fail_push=False):
        self.reported = reported
        self.fail_push = fail_push

    def __call__(self, args, *, cwd=None, check=True, env=None, timeout=None):
        if list(args[:2]) == ["nix", "build"]:
            binary = Path(cwd) / "result" / "bin" / "dummy"
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_text(f"#!/bin/sh\necho {self.reported}\n")
            binary.chmod(0o755)
            return common.CommandResult(args, 0, "", "")
        if self.fail_push and list(args[:2]) == ["git", "push"]:
            if check:
                raise common.UpdateError("simulated push failure")
            return common.CommandResult(args, 1, "", "simulated push failure")
        return shell(args, cwd=cwd, check=check, env=env, timeout=timeout)


class CommitDuringBuildCommand(FakeBuildCommand):
    def __init__(self, root):
        super().__init__()
        self.root = root
        self.committed = False

    def __call__(self, args, **kwargs):
        if list(args[:2]) == ["nix", "build"] and not self.committed:
            self.committed = True
            (self.root / "data.json").write_text('{"concurrent": true}\n')
            shell(["git", "add", "data.json"], cwd=self.root)
            shell(["git", "commit", "-m", "concurrent commit"], cwd=self.root)
        return super().__call__(args, **kwargs)


class StageForeignContentCommand(FakeBuildCommand):
    def __init__(self, root):
        super().__init__()
        self.root = root
        self.tampered = False

    def __call__(self, args, **kwargs):
        result = super().__call__(args, **kwargs)
        if list(args[:2]) == ["git", "add"] and not self.tampered:
            self.tampered = True
            (self.root / "package.nix").write_text('version = "7.7.7";\n')
            shell(["git", "add", "package.nix"], cwd=self.root)
        return result


class CommonUnitTests(unittest.TestCase):
    def test_rejects_malformed_versions_and_json_null(self):
        with self.assertRaises(common.UpdateError):
            common.Version.parse("1.2.3-rc.1")

        def fake(args, **kwargs):
            return common.CommandResult(args, 0, "null", "")

        ctx = common.Context(Path("/tmp"), Path("/tmp"), fake)
        with self.assertRaisesRegex(common.UpdateError, "null JSON"):
            ctx.http_json("https://example.invalid/data")

    def test_hash_is_tied_to_named_derivation(self):
        digest = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
        output = (
            "error: hash mismatch in fixed-output derivation '/nix/store/a-right-name.drv':\n"
            f"  got: {digest}\n"
        )
        self.assertEqual(common.extract_mismatch_hash(output, "right-name"), digest)
        with self.assertRaises(common.UpdateError):
            common.extract_mismatch_hash(output, "other-name")

    def test_version_output_allows_repeated_same_version_but_no_conflict(self):
        version = common.Version.parse("2.100.0")
        self.assertTrue(common.exact_version_in_output("gh version 2.100.0\nhttps://x/v2.100.0", version))
        self.assertFalse(common.exact_version_in_output("gh version 2.100.0 DEV", version))
        self.assertFalse(common.exact_version_in_output("2.100.0 and 2.99.0", version))

    def test_lock_inode_persists_and_excludes_second_holder(self):
        fixture = GitFixture()
        try:
            with common.repository_lock(fixture.root):
                lock = fixture.root / ".git" / "selfhost-update.lock"
                inode = lock.stat().st_ino
                with self.assertRaisesRegex(common.UpdateError, "already running"):
                    with common.repository_lock(fixture.root):
                        pass
            self.assertTrue(lock.exists())
            self.assertEqual(lock.stat().st_ino, inode)
            with common.repository_lock(fixture.root):
                self.assertEqual(lock.stat().st_ino, inode)
        finally:
            fixture.close()


class RunnerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = GitFixture()

    def tearDown(self):
        self.fixture.close()

    def test_check_is_read_only_and_tolerates_dirty_untracked_tree(self):
        dirty = self.fixture.root / "notes.txt"
        dirty.write_text("user work\n")
        before = {path.relative_to(self.fixture.root): path.read_bytes() for path in self.fixture.root.rglob("*") if path.is_file()}
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = common.Runner(DummyProfile(), self.fixture.root).run(["--check"])
        after = {path.relative_to(self.fixture.root): path.read_bytes() for path in self.fixture.root.rglob("*") if path.is_file()}
        self.assertEqual(result, 0)
        self.assertEqual(before, after)
        self.assertIn("uncommitted changes", output.getvalue())

    def test_normal_run_rejects_dirty_and_untracked_files(self):
        (self.fixture.root / "notes.txt").write_text("user work\n")
        with self.assertRaisesRegex(common.UpdateError, "dirty"):
            common.Runner(DummyProfile(), self.fixture.root).run(["--no-push"])

    def test_normal_run_rejects_tracked_user_change(self):
        (self.fixture.root / "package.nix").write_text('version = "1.0.1";\n')
        with self.assertRaisesRegex(common.UpdateError, "dirty"):
            common.Runner(DummyProfile(), self.fixture.root).run(["--no-push"])

    def test_wrong_binary_version_never_changes_main_tree(self):
        original = (self.fixture.root / "package.nix").read_bytes()
        runner = common.Runner(DummyProfile(), self.fixture.root, FakeBuildCommand("9.9.9"))
        with self.assertRaisesRegex(common.UpdateError, "wrong version"):
            runner.run(["--no-push"])
        self.assertEqual((self.fixture.root / "package.nix").read_bytes(), original)
        self.assertFalse(shell(["git", "status", "--porcelain"], cwd=self.fixture.root).stdout)

    def test_profile_cannot_change_non_allowlisted_candidate_file(self):
        runner = common.Runner(DummyProfile(extra_path="outside.txt"), self.fixture.root, FakeBuildCommand())
        with self.assertRaisesRegex(common.UpdateError, "outside its allowlist"):
            runner.run(["--no-push"])
        self.assertFalse((self.fixture.root / "outside.txt").exists())

    def test_clean_concurrent_commit_is_detected_before_transfer(self):
        command = CommitDuringBuildCommand(self.fixture.root)
        runner = common.Runner(DummyProfile(), self.fixture.root, command)
        with self.assertRaisesRegex(common.UpdateError, "branch or HEAD changed"):
            runner.run(["--no-push"])
        self.assertEqual((self.fixture.root / "package.nix").read_text(), 'version = "1.0.0";\n')
        self.assertEqual((self.fixture.root / "data.json").read_text(), '{"concurrent": true}\n')
        self.assertFalse(shell(["git", "status", "--porcelain"], cwd=self.fixture.root).stdout)

    def test_foreign_staged_change_survives_safe_rollback(self):
        command = StageForeignContentCommand(self.fixture.root)
        runner = common.Runner(DummyProfile(), self.fixture.root, command)
        with self.assertRaisesRegex(common.UpdateError, "preserved concurrent changes"):
            runner.run(["--no-push"])
        self.assertEqual((self.fixture.root / "package.nix").read_text(), 'version = "7.7.7";\n')
        staged = shell(["git", "show", ":package.nix"], cwd=self.fixture.root).stdout
        self.assertEqual(staged, 'version = "7.7.7";\n')

    def test_partial_transfer_failure_restores_every_file_and_modes(self):
        package = self.fixture.root / "package.nix"
        data = self.fixture.root / "data.json"
        originals = package.read_bytes(), data.read_bytes()
        modes = package.stat().st_mode & 0o7777, data.stat().st_mode & 0o7777
        real_atomic = common.atomic_write
        writes = 0

        def fail_second(path, value):
            nonlocal writes
            writes += 1
            if writes == 2:
                raise OSError("simulated second-file failure")
            real_atomic(path, value)

        runner = common.Runner(DummyProfile(), self.fixture.root, FakeBuildCommand())
        with mock.patch.object(common, "atomic_write", side_effect=fail_second):
            with self.assertRaisesRegex(OSError, "second-file"):
                runner.run(["--no-push"])
        self.assertEqual((package.read_bytes(), data.read_bytes()), originals)
        self.assertEqual((package.stat().st_mode & 0o7777, data.stat().st_mode & 0o7777), modes)
        self.assertFalse(shell(["git", "status", "--porcelain"], cwd=self.fixture.root).stdout)

    def test_failed_push_is_recovered_only_with_verified_marker(self):
        runner = common.Runner(DummyProfile(), self.fixture.root, FakeBuildCommand(fail_push=True))
        with self.assertRaisesRegex(common.UpdateError, "push failure"):
            runner.run([])
        marker = self.fixture.root / ".git" / "selfhost-update-pending.json"
        self.assertTrue(marker.exists())
        pending = json.loads(marker.read_text())["commit"]
        self.assertEqual(pending, shell(["git", "rev-parse", "HEAD"], cwd=self.fixture.root).stdout.strip())

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            common.Runner(DummyProfile(), self.fixture.root).run([])
        self.assertFalse(marker.exists())
        self.assertIn("Recovered pending", output.getvalue())

    def test_unknown_local_commit_is_never_auto_pushed(self):
        (self.fixture.root / "data.json").write_text('{"user": true}\n')
        shell(["git", "add", "data.json"], cwd=self.fixture.root)
        shell(["git", "commit", "-m", "user commit"], cwd=self.fixture.root)
        with self.assertRaisesRegex(common.UpdateError, "unknown commits"):
            common.Runner(DummyProfile(), self.fixture.root).run([])
        remote = shell(["git", "rev-parse", "refs/heads/main"], cwd=self.fixture.remote).stdout.strip()
        local_parent = shell(["git", "rev-parse", "HEAD^"], cwd=self.fixture.root).stdout.strip()
        self.assertEqual(remote, local_parent)


if __name__ == "__main__":
    unittest.main()
