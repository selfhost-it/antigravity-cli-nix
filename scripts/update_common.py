#!/usr/bin/env python3
"""Deterministic updater engine shared by the self-host.it package repositories.

Adapter API
===========

``main(Profile())`` accepts a profile with these attributes and methods:

* ``name``, ``files``, ``binary`` and optionally ``package_attr`` / ``version_args``;
* ``current_version(root) -> Version``;
* ``discover(ctx, requested: Version | None) -> Target``;
* ``prepare(ctx, target)`` (edits only ``ctx.workdir``);
* optional ``version_matches(output, target)`` and ``commit_subject(target)``.

The engine owns repository validation, the repository-local lock, an isolated
candidate checkout, final Nix build and exact binary-version validation, an
allowlisted transfer, staged-snapshot secret scanning, commit/push and recovery.
Profiles can use ``Context.http_json``, ``http_bytes``, ``prefetch``, ``build``,
``replace_one`` and ``write_bytes``.  This file uses only the Python standard
library and the git, nix, curl and ssh commands.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import dataclasses
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Callable, Iterable, Sequence


HTTP_CONNECT_TIMEOUT = 10
HTTP_TIMEOUT = 30
BUILD_TIMEOUT = 60 * 60
MIN_FREE_BYTES = 2 * 1024**3
FAKE_SHA256 = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
SRI_RE = re.compile(r"sha(?:256|512)-[A-Za-z0-9+/]+={0,2}")


class UpdateError(RuntimeError):
    """A diagnosed updater failure suitable for display to the operator."""


@dataclasses.dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, value: str) -> "Version":
        match = re.fullmatch(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", value)
        if not match:
            raise UpdateError(f"invalid stable semantic version: {value!r}")
        return cls(*(int(part) for part in match.groups()))

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclasses.dataclass(frozen=True)
class Target:
    version: Version
    payload: Any = None


class Profile:
    """Optional base class documenting defaults available to adapters."""

    package_attr = "."
    version_args = ("--version",)

    def is_current(self, root: Path, target: Target) -> bool:
        return self.current_version(root) == target.version

    def version_matches(self, output: str, target: Target) -> bool:
        return exact_version_in_output(output, target.version)

    def commit_subject(self, target: Target) -> str:
        return f"Update {self.name} to v{target.version}"


@dataclasses.dataclass
class CommandResult:
    args: Sequence[str]
    returncode: int
    stdout: str
    stderr: str


Command = Callable[..., CommandResult]


def run_command(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    env: dict[str, str] | None = None,
    timeout: int | None = None,
) -> CommandResult:
    try:
        completed = subprocess.run(
            list(args),
            cwd=cwd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            env=env,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateError(f"failed to run {' '.join(args)}: {exc}") from exc
    result = CommandResult(args, completed.returncode, completed.stdout, completed.stderr)
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise UpdateError(f"command failed ({result.returncode}): {' '.join(args)}\n{detail}")
    return result


class Context:
    """Services exposed to a package profile.

    During discovery ``workdir`` equals ``root``.  During preparation it is the
    isolated candidate tree.  Profiles must never edit ``root`` directly.
    """

    def __init__(self, root: Path, workdir: Path, command: Command = run_command):
        self.root = root
        self.workdir = workdir
        self.command = command

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> CommandResult:
        return self.command(
            args,
            cwd=cwd or self.workdir,
            check=check,
            env=env,
            timeout=timeout,
        )

    def http_bytes(self, url: str) -> bytes:
        result = self.run(
            [
                "curl", "--fail", "--silent", "--show-error", "--location",
                "--connect-timeout", str(HTTP_CONNECT_TIMEOUT),
                "--max-time", str(HTTP_TIMEOUT),
                url,
            ],
            cwd=self.root,
            timeout=HTTP_TIMEOUT + 5,
        )
        if not result.stdout:
            raise UpdateError(f"empty HTTP response from {url}")
        return result.stdout.encode()

    def http_json(self, url: str) -> Any:
        raw = self.http_bytes(url)
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpdateError(f"malformed JSON from {url}: {exc}") from exc
        if value is None:
            raise UpdateError(f"null JSON response from {url}")
        return value

    def prefetch(self, url: str, *, unpack: bool) -> str:
        args = ["nix", "store", "prefetch-file", "--json"]
        if unpack:
            args.append("--unpack")
        args.append(url)
        result = self.run(args, timeout=HTTP_TIMEOUT * 4)
        try:
            payload = json.loads(result.stdout)
            value = payload["hash"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise UpdateError("nix prefetch returned malformed JSON") from exc
        validate_sri(value)
        return value

    def replace_one(self, relative: str, pattern: str, replacement: str, *, flags: int = 0) -> None:
        path = self.workdir / relative
        original = path.read_text()
        updated, count = re.subn(pattern, replacement, original, count=1, flags=flags)
        if count != 1:
            raise UpdateError(f"expected exactly one match in {relative}: {pattern}")
        path.write_text(updated)

    def write_bytes(self, relative: str, value: bytes) -> None:
        path = self.workdir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)

    def build(self, package_attr: str = ".", *, expect_hash_for: str | None = None) -> str | None:
        target = package_attr if package_attr.startswith(".") else f".#{package_attr}"
        result = self.run(
            ["nix", "build", "--no-write-lock-file", target, "--out-link", "result"],
            check=expect_hash_for is None,
            timeout=BUILD_TIMEOUT,
        )
        if expect_hash_for is None:
            return None
        if result.returncode == 0:
            raise UpdateError(f"build unexpectedly succeeded while learning {expect_hash_for}")
        return extract_mismatch_hash(result.stdout + "\n" + result.stderr, expect_hash_for)


def validate_sri(value: str, algorithm: str | None = None) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"sha(?:256|512)-[A-Za-z0-9+/]+={0,2}", value):
        raise UpdateError(f"invalid SRI hash: {value!r}")
    prefix, encoded = value.split("-", 1)
    if algorithm and prefix != algorithm:
        raise UpdateError(f"expected {algorithm} SRI hash, got {prefix}")
    expected = {"sha256": 32, "sha512": 64}[prefix]
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise UpdateError(f"invalid base64 in SRI hash: {value!r}") from exc
    if len(decoded) != expected:
        raise UpdateError(f"invalid {prefix} digest length")
    return value


def extract_mismatch_hash(output: str, derivation_hint: str) -> str:
    """Extract `got:` only from a hash-mismatch block naming the derivation."""
    blocks = re.split(r"(?=error: hash mismatch in fixed-output derivation)", output)
    matches: list[str] = []
    for block in blocks:
        header = block.splitlines()[0] if block.splitlines() else ""
        if "hash mismatch in fixed-output derivation" not in header:
            continue
        if derivation_hint not in header:
            continue
        got = re.search(r"(?m)^\s*got:\s*(sha(?:256|512)-[A-Za-z0-9+/]+={0,2})\s*$", block)
        if got:
            matches.append(validate_sri(got.group(1)))
    if len(matches) != 1:
        raise UpdateError(
            f"could not identify one hash mismatch for {derivation_hint!r}; "
            f"found {len(matches)}"
        )
    return matches[0]


def learn_hashes(
    ctx: Context,
    setters: dict[str, tuple[str, Callable[[str], None]]],
    package_attr: str = ".",
) -> dict[str, str]:
    """Learn a set of source-dependent FOD hashes without reusing stale ones.

    Every hash is invalidated before the first build.  Each mismatch is accepted
    only from a block whose derivation name contains its profile-provided hint.
    """
    for _name, (_hint, setter) in setters.items():
        setter(FAKE_SHA256)
    pending = dict(setters)
    learned: dict[str, str] = {}
    target = package_attr if package_attr.startswith(".") else f".#{package_attr}"
    while pending:
        result = ctx.run(
            ["nix", "build", "--no-write-lock-file", target, "--out-link", "result"],
            check=False,
            timeout=BUILD_TIMEOUT,
        )
        if result.returncode == 0:
            raise UpdateError(f"build succeeded before hashes were learned: {', '.join(pending)}")
        output = result.stdout + "\n" + result.stderr
        progress = False
        for name, (hint, setter) in list(pending.items()):
            try:
                value = extract_mismatch_hash(output, hint)
            except UpdateError as exc:
                if "found 0" in str(exc):
                    continue
                raise
            setter(value)
            learned[name] = value
            del pending[name]
            progress = True
        if not progress:
            detail = output.strip()[-4000:]
            raise UpdateError(f"build failed without an attributable pending hash mismatch:\n{detail}")
    return learned


def github_stable_releases(ctx: Context, repository: str, *, exclude_prefixes: tuple[str, ...] = ()) -> list[Version]:
    versions: set[Version] = set()
    for page in range(1, 21):
        url = f"https://api.github.com/repos/{repository}/releases?per_page=100&page={page}"
        payload = ctx.http_json(url)
        if not isinstance(payload, list):
            raise UpdateError(f"GitHub releases response for {repository} is not an array")
        for release in payload:
            if not isinstance(release, dict):
                raise UpdateError(f"malformed GitHub release entry for {repository}")
            tag = release.get("tag_name")
            if not isinstance(tag, str) or not isinstance(release.get("draft"), bool) or not isinstance(release.get("prerelease"), bool):
                raise UpdateError(f"malformed GitHub release schema for {repository}")
            if release["draft"] or release["prerelease"] or tag.startswith(exclude_prefixes):
                continue
            try:
                versions.add(Version.parse(tag))
            except UpdateError:
                continue
        if len(payload) < 100:
            break
    if not versions:
        raise UpdateError(f"no stable semantic-version releases found for {repository}")
    return sorted(versions, reverse=True)


def select_release(
    ctx: Context,
    repository: str,
    requested: Version | None,
    *,
    exclude_prefixes: tuple[str, ...] = (),
) -> Version:
    versions = github_stable_releases(ctx, repository, exclude_prefixes=exclude_prefixes)
    if requested is not None:
        if requested not in versions:
            raise UpdateError(f"v{requested} is not a published stable release of {repository}")
        return requested
    return versions[0]


def package_version(root: Path) -> Version:
    text = (root / "package.nix").read_text()
    matches = re.findall(r'(?m)^\s*version\s*=\s*"([^"]+)";', text)
    if len(matches) != 1:
        raise UpdateError("package.nix must contain exactly one version assignment")
    return Version.parse(matches[0])


def set_package_version(ctx: Context, version: Version) -> None:
    ctx.replace_one("package.nix", r'(?m)^(\s*version\s*=\s*)"[^"]+";', rf'\g<1>"{version}";')


def set_source_hash(ctx: Context, value: str) -> None:
    validate_sri(value, "sha256")
    ctx.replace_one(
        "package.nix",
        r'(rev\s*=\s*"v\$\{version\}";\s*\n\s*hash\s*=\s*)"[^"]*";',
        rf'\g<1>"{value}";',
    )


def set_named_hash(ctx: Context, name: str, value: str) -> None:
    validate_sri(value)
    ctx.replace_one("package.nix", rf'(?m)^(\s*{re.escape(name)}\s*=\s*)"[^"]*";', rf'\g<1>"{value}";')


def source_hash(ctx: Context, repository: str, version: Version) -> str:
    return ctx.prefetch(f"https://github.com/{repository}/archive/refs/tags/v{version}.tar.gz", unpack=True)


def learn_named_hash(ctx: Context, field: str, derivation_hint: str, package_attr: str = ".") -> str:
    set_named_hash(ctx, field, FAKE_SHA256)
    learned = ctx.build(package_attr, expect_hash_for=derivation_hint)
    assert learned is not None
    set_named_hash(ctx, field, learned)
    return learned


@contextlib.contextmanager
def repository_lock(root: Path, command: Command = run_command) -> Iterable[None]:
    resolved = command(
        ["git", "rev-parse", "--git-path", "selfhost-update.lock"],
        cwd=root,
        check=True,
    ).stdout.strip()
    lock_path = Path(resolved)
    if not lock_path.is_absolute():
        lock_path = root / lock_path
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise UpdateError("another updater is already running for this repository") from exc
        yield
    finally:
        handle.close()


class Runner:
    def __init__(self, profile: Any, root: Path | None = None, command: Command = run_command):
        self.profile = profile
        self.root = (root or Path(__file__).resolve().parents[1]).resolve()
        self.command = command
        self._baseline_head: str | None = None
        self._baseline_branch: str | None = None

    def cmd(self, args: Sequence[str], **kwargs: Any) -> CommandResult:
        return self.command(args, cwd=kwargs.pop("cwd", self.root), **kwargs)

    def run(self, argv: Sequence[str] | None = None) -> int:
        args = parse_args(argv)
        requested = Version.parse(args.version) if args.version else None
        current = self.profile.current_version(self.root)
        discovery = Context(self.root, self.root, self.command)

        if args.check or args.dry_run:
            target = self.profile.discover(discovery, requested)
            ensure_not_downgrade(current, target.version)
            current_matches = self._is_current(target)
            notes = self._readonly_notes()
            if current_matches:
                print(f"{self.profile.name} is up to date at v{current}{notes}")
            else:
                mode = "Would update" if args.dry_run else "Update available"
                print(f"{mode}: {self.profile.name} v{current} -> v{target.version}")
                if notes:
                    print(notes.lstrip("; "))
            return 0

        with repository_lock(self.root, self.command):
            self._preflight(push=not args.no_push)
            current = self.profile.current_version(self.root)
            target = self.profile.discover(discovery, requested)
            ensure_not_downgrade(current, target.version)
            if self._is_current(target):
                print(f"{self.profile.name} is up to date at v{current}")
                return 0
            self._check_space()
            self._perform_update(target, push=not args.no_push)
            return 0

    def _is_current(self, target: Target) -> bool:
        checker = getattr(self.profile, "is_current", None)
        return checker(self.root, target) if checker else self.profile.current_version(self.root) == target.version

    def _readonly_notes(self) -> str:
        """Report local state without changing refs, index, locks or worktree."""
        notes: list[str] = []
        try:
            if self._readonly_git("status", "--porcelain", "--untracked-files=all").stdout:
                notes.append("working tree has uncommitted changes")
            branch = self._readonly_git("symbolic-ref", "--quiet", "--short", "HEAD", check=False).stdout.strip()
            remote_lines = self._remote_git("ls-remote", "--heads", "origin", "refs/heads/main", check=False).stdout.splitlines()
            local = self._readonly_git("rev-parse", "HEAD", check=False).stdout.strip()
            if branch == "main" and len(remote_lines) == 1 and re.fullmatch(r"[0-9a-f]{40}\s+refs/heads/main", remote_lines[0]):
                remote = remote_lines[0].split()[0]
                if local != remote and self._readonly_git("merge-base", "--is-ancestor", remote, local, check=False).returncode == 0:
                    notes.append("local main has commits pending push")
        except UpdateError:
            notes.append("Git remote state could not be determined read-only")
        return f"; {'; '.join(notes)}" if notes else ""

    def _git(self, *args: str, check: bool = True) -> CommandResult:
        return self.cmd(["git", *args], check=check)

    def _readonly_git(self, *args: str, check: bool = True) -> CommandResult:
        env = os.environ.copy()
        env["GIT_OPTIONAL_LOCKS"] = "0"
        return self.cmd(["git", "--no-optional-locks", *args], check=check, env=env)

    def _git_path(self, name: str) -> Path:
        value = self._git("rev-parse", "--git-path", name).stdout.strip()
        path = Path(value)
        return path if path.is_absolute() else self.root / path

    def _remote_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.setdefault(
            "GIT_SSH_COMMAND",
            f"ssh -i {Path.home() / '.ssh' / 'self-host-github'} -o BatchMode=yes -o ConnectTimeout=10",
        )
        env.setdefault("GIT_TERMINAL_PROMPT", "0")
        return env

    def _remote_git(self, *args: str, check: bool = True) -> CommandResult:
        return self.cmd(
            ["git", *args],
            check=check,
            env=self._remote_env(),
            timeout=45,
        )

    def _pending_marker(self) -> Path:
        return self._git_path("selfhost-update-pending.json")

    def _read_pending_marker(self) -> dict[str, str] | None:
        path = self._pending_marker()
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise UpdateError(f"invalid pending-update marker at {path}: {exc}") from exc
        if not isinstance(payload, dict) or not re.fullmatch(r"[0-9a-f]{40}", payload.get("commit", "")):
            raise UpdateError(f"invalid pending-update marker at {path}")
        return payload

    def _write_pending_marker(self, commit: str) -> None:
        payload = {"commit": commit, "profile": self.profile.name}
        atomic_write(self._pending_marker(), (json.dumps(payload, sort_keys=True) + "\n").encode())

    def _preflight(self, *, push: bool) -> None:
        if self._git("rev-parse", "--show-toplevel").stdout.strip() != str(self.root):
            raise UpdateError("updater must run from its own Git repository")
        branch = self._git("symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
        if branch != "main":
            raise UpdateError(f"expected branch main, found {branch or 'detached HEAD'}")
        if self._git("config", "--get", "branch.main.remote").stdout.strip() != "origin":
            raise UpdateError("main must track remote origin")
        if self._git("config", "--get", "branch.main.merge").stdout.strip() != "refs/heads/main":
            raise UpdateError("main must track refs/heads/main")
        status = self._git("status", "--porcelain", "--untracked-files=all").stdout
        if status:
            raise UpdateError("working tree or index is dirty; refusing to overwrite user changes")

        remote_lines = self._remote_git("ls-remote", "--heads", "origin", "refs/heads/main").stdout.splitlines()
        if len(remote_lines) != 1 or not re.fullmatch(r"[0-9a-f]{40}\s+refs/heads/main", remote_lines[0]):
            raise UpdateError("origin did not report exactly one refs/heads/main")
        remote = remote_lines[0].split()[0]
        local = self._git("rev-parse", "HEAD").stdout.strip()
        if local == remote:
            with contextlib.suppress(FileNotFoundError):
                self._pending_marker().unlink()
            self._baseline_head, self._baseline_branch = local, branch
            return
        known = self._git("cat-file", "-e", f"{remote}^{{commit}}", check=False).returncode == 0
        if not known:
            self._remote_git("fetch", "--no-tags", "origin", "main")
        if self._git("merge-base", "--is-ancestor", remote, local, check=False).returncode == 0:
            marker = self._read_pending_marker()
            if marker is None or marker["commit"] != local or marker.get("profile") != self.profile.name:
                raise UpdateError(
                    "local main is ahead of origin/main without a matching verified-update marker; "
                    "refusing to push unknown commits"
                )
            scan_index(self.root, self.command)
            if push:
                self._push()
                print("Recovered pending local commit by pushing it to origin/main")
            else:
                print("Local main has commits pending push; --no-push leaves them local")
            self._baseline_head, self._baseline_branch = local, branch
            return
        if self._git("merge-base", "--is-ancestor", local, remote, check=False).returncode == 0:
            raise UpdateError("local main is behind origin/main; update it before running the updater")
        raise UpdateError("local main has diverged from origin/main")

    def _check_space(self) -> None:
        for path in {self.root, Path("/nix/store") if Path("/nix/store").exists() else self.root}:
            free = shutil.disk_usage(path).free
            if free < MIN_FREE_BYTES:
                raise UpdateError(f"less than 2 GiB free on filesystem containing {path}")

    def _tracked_checkout(self, destination: Path) -> None:
        raw = self._git("ls-files", "-z").stdout
        for name in filter(None, raw.split("\0")):
            source = self.root / name
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_symlink():
                target.symlink_to(os.readlink(source))
            else:
                shutil.copy2(source, target)

    def _perform_update(self, target: Target, *, push: bool) -> None:
        files = tuple(self.profile.files)
        validate_allowlist(files)
        committed = False
        transferred = False
        backups = {name: (self.root / name).read_bytes() for name in files}
        expected: dict[str, bytes] = {}
        try:
            with tempfile.TemporaryDirectory(prefix=f"{self.profile.name}-update-") as temp:
                workdir = Path(temp)
                self._tracked_checkout(workdir)
                context = Context(self.root, workdir, self.command)
                self.profile.prepare(context, target)
                self._assert_candidate_allowlist(workdir, files)
                context.build(getattr(self.profile, "package_attr", "."))
                self._verify_candidate(context, target)
                self._assert_clean()
                for name in files:
                    candidate = workdir / name
                    if not candidate.is_file():
                        raise UpdateError(f"profile did not produce allowlisted file {name}")
                expected = {name: (workdir / name).read_bytes() for name in files}
                transferred = True
                for name in files:
                    atomic_write(self.root / name, expected[name])

            self._assert_identity()
            self._assert_only_allowlisted(files)
            self._git("add", "--", *files)
            staged = set(filter(None, self._git("diff", "--cached", "--name-only", "-z").stdout.split("\0")))
            expected_changed = {name for name in files if expected[name] != backups[name]}
            if not staged or staged != expected_changed:
                raise UpdateError(
                    f"staged paths differ from verified candidate: expected {sorted(expected_changed)}, "
                    f"got {sorted(staged)}"
                )
            for name in staged:
                indexed = self._git("show", f":{name}").stdout.encode(errors="surrogateescape")
                if indexed != expected[name]:
                    raise UpdateError(f"staged content for {name} differs from the verified candidate")
            if self._git("diff", "--name-only").stdout:
                raise UpdateError("working-tree content changed after staging the verified candidate")
            self._assert_identity()
            scan_index(self.root, self.command)
            subject = self.profile.commit_subject(target)
            self._git("commit", "-m", subject)
            committed = True
            commit = self._git("rev-parse", "HEAD").stdout.strip()
            self._write_pending_marker(commit)
            if push:
                self._push()
            else:
                print("Created update commit; --no-push left it local")
        except Exception as exc:
            if transferred and not committed:
                conflicts = self._rollback_owned(files, backups, expected)
                if conflicts:
                    raise UpdateError(
                        f"{exc}; rollback preserved concurrent changes in: {', '.join(conflicts)}; "
                        "inspect the repository manually"
                    ) from exc
            raise

    def _rollback_owned(
        self,
        files: Sequence[str],
        backups: dict[str, bytes],
        expected: dict[str, bytes],
    ) -> list[str]:
        """Undo only bytes and index entries still provably written by us."""
        identity_same = self._identity_matches()
        conflicts: list[str] = []
        for name in files:
            original = backups[name]
            candidate = expected.get(name)
            if candidate is None or candidate == original:
                continue
            path = self.root / name
            current = path.read_bytes() if path.exists() else None
            indexed_result = self._git("show", f":{name}", check=False)
            indexed = indexed_result.stdout.encode(errors="surrogateescape") if indexed_result.returncode == 0 else None

            if current == candidate:
                restore = original
                if not identity_same:
                    head = self._git("show", f"HEAD:{name}", check=False)
                    if head.returncode:
                        conflicts.append(f"worktree:{name}")
                        restore = None
                    else:
                        restore = head.stdout.encode(errors="surrogateescape")
                if restore is not None:
                    atomic_write(path, restore)
            elif current != original:
                conflicts.append(f"worktree:{name}")

            if identity_same and indexed == candidate:
                self._git("reset", "-q", "HEAD", "--", name, check=False)
            elif indexed not in (original, candidate):
                conflicts.append(f"index:{name}")
        return conflicts

    def _identity_matches(self) -> bool:
        if self._baseline_head is None or self._baseline_branch is None:
            return False
        branch = self._git("symbolic-ref", "--quiet", "--short", "HEAD", check=False).stdout.strip()
        head = self._git("rev-parse", "HEAD", check=False).stdout.strip()
        return branch == self._baseline_branch and head == self._baseline_head

    def _assert_identity(self) -> None:
        if not self._identity_matches():
            raise UpdateError("branch or HEAD changed while the candidate was building")

    def _assert_candidate_allowlist(self, workdir: Path, files: Sequence[str]) -> None:
        allowed = set(files)
        tracked = set(filter(None, self._git("ls-files", "-z").stdout.split("\0")))
        present = {
            str(path.relative_to(workdir))
            for path in workdir.rglob("*")
            if not path.is_dir()
        }
        changed: set[str] = set()
        for name in tracked | present:
            source = self.root / name
            candidate = workdir / name
            if not source.exists() and not source.is_symlink():
                changed.add(name)
            elif not candidate.exists() and not candidate.is_symlink():
                changed.add(name)
            elif source.is_symlink() != candidate.is_symlink():
                changed.add(name)
            elif source.is_symlink():
                if os.readlink(source) != os.readlink(candidate):
                    changed.add(name)
            elif source.read_bytes() != candidate.read_bytes():
                changed.add(name)
        if not changed or not changed.issubset(allowed):
            raise UpdateError(f"profile changed paths outside its allowlist: {sorted(changed - allowed)}")

    def _verify_candidate(self, context: Context, target: Target) -> None:
        result = context.run([str(context.workdir / "result" / "bin" / self.profile.binary), *getattr(self.profile, "version_args", ("--version",))])
        output = (result.stdout + "\n" + result.stderr).strip()
        matcher = getattr(self.profile, "version_matches", None)
        valid = matcher(output, target) if matcher else exact_version_in_output(output, target.version)
        if not valid:
            raise UpdateError(f"built binary reported the wrong version: {output!r}")

    def _assert_clean(self) -> None:
        self._assert_identity()
        if self._git("status", "--porcelain", "--untracked-files=all").stdout:
            raise UpdateError("repository changed while candidate was building")

    def _assert_only_allowlisted(self, files: Sequence[str]) -> None:
        allowed = set(files)
        changed = set(filter(None, self._git("diff", "--name-only", "-z").stdout.split("\0")))
        untracked = set(filter(None, self._git("ls-files", "--others", "--exclude-standard", "-z").stdout.split("\0")))
        if not changed or not changed.issubset(allowed) or untracked:
            raise UpdateError(f"unexpected paths changed during transfer: {sorted(changed | untracked)}")

    def _push(self) -> None:
        self._remote_git("push", "origin", "HEAD:refs/heads/main")
        with contextlib.suppress(FileNotFoundError):
            self._pending_marker().unlink()


def validate_allowlist(files: Sequence[str]) -> None:
    if not files:
        raise UpdateError("profile allowlist is empty")
    for name in files:
        path = Path(name)
        if path.is_absolute() or ".." in path.parts or name.startswith(".git/"):
            raise UpdateError(f"unsafe allowlisted path: {name}")


def atomic_write(path: Path, value: bytes) -> None:
    previous_mode = path.stat().st_mode & 0o7777 if path.exists() else None
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            if previous_mode is not None:
                os.fchmod(handle.fileno(), previous_mode)
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def ensure_not_downgrade(current: Version, candidate: Version) -> None:
    if candidate < current:
        raise UpdateError(f"refusing downgrade from v{current} to v{candidate}")


def exact_version_in_output(output: str, version: Version) -> bool:
    wanted = str(version)
    tokens = re.findall(r"(?<!\d)(\d+\.\d+\.\d+)(?![\d.-])", output)
    return bool(tokens) and all(token == wanted for token in tokens) and "DEV" not in output


SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:OPENSSH|RSA|EC|DSA) PRIVATE KEY-----"),
    re.compile(rb"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b"),
    re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{30,}\b"),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(rb"\bsk-[A-Za-z0-9]{32,}\b"),
    re.compile(rb"(?i)(?:password|passwd|api[_-]?key|secret|token)\s*[:=]\s*['\"]?[A-Za-z0-9+/_.-]{20,}"),
)


def scan_index(root: Path, command: Command = run_command) -> None:
    listed = command(["git", "ls-files", "-z"], cwd=root, check=True).stdout
    findings: list[str] = []
    for name in filter(None, listed.split("\0")):
        result = command(["git", "show", f":{name}"], cwd=root, check=False)
        if result.returncode:
            raise UpdateError(f"could not read staged snapshot for {name}")
        data = result.stdout.encode(errors="surrogateescape")
        if b"\0" in data:
            continue
        if any(pattern.search(data) for pattern in SECRET_PATTERNS):
            findings.append(name)
    if findings:
        raise UpdateError(f"possible sensitive data in staged snapshot: {', '.join(findings)}")


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deterministically update this Nix package")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="read-only update availability check")
    modes.add_argument("--dry-run", action="store_true", help="read-only description of the candidate update")
    parser.add_argument("--no-push", action="store_true", help="commit the verified update without pushing")
    parser.add_argument("--version", metavar="VERSION", help="use a published stable version instead of latest")
    args = parser.parse_args(argv)
    if args.no_push and (args.check or args.dry_run):
        parser.error("--no-push only applies to a real update")
    return args


def main(profile: Any, argv: Sequence[str] | None = None) -> int:
    try:
        return Runner(profile).run(argv)
    except (UpdateError, OSError, ValueError) as exc:
        print(f"update failed: {exc}", file=sys.stderr)
        return 1
