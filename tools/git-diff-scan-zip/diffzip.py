#!/usr/bin/env python3
"""
Create a ZIP containing only files changed on a local Git branch.

Designed for preparing focused source bundles for security/static-analysis tools
such as Checkmarx, and for staging partial source trees used by Sonar scanners.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from datetime import datetime, timezone
from typing import Iterable
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

VERSION = "1.0.0"
META_DIR = ".diffscan"

CONTEXT_PATTERNS = (
    "sonar-project.properties",
    ".sonarcloud.properties",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "settings.gradle.kts",
    "gradle.properties",
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "requirements*.txt",
    "pyproject.toml",
    "Pipfile",
    "Pipfile.lock",
    "poetry.lock",
    "go.mod",
    "go.sum",
    "Cargo.toml",
    "Cargo.lock",
    "*.sln",
    "*.csproj",
    "*.fsproj",
    "*.vbproj",
    "Directory.Build.props",
    "Directory.Build.targets",
)


class GitError(RuntimeError):
    pass


def run_git(
    repo: Path,
    args: list[str],
    *,
    input_bytes: bytes | None = None,
    allow_fail: bool = False,
) -> bytes:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0 and not allow_fail:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise GitError(f"git {' '.join(args)} failed: {stderr}")
    return proc.stdout


def text_git(repo: Path, args: list[str], *, allow_fail: bool = False) -> str:
    return run_git(repo, args, allow_fail=allow_fail).decode(
        "utf-8", errors="replace"
    ).strip()


def find_repo_root(path: Path) -> Path:
    path = path.expanduser().resolve()
    root = text_git(path, ["rev-parse", "--show-toplevel"])
    return Path(root).resolve()


def ref_exists(repo: Path, ref: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc.returncode == 0


def resolve_commit(repo: Path, ref: str) -> str:
    return text_git(repo, ["rev-parse", "--verify", f"{ref}^{{commit}}"])


def current_target(repo: Path) -> str:
    branch = text_git(
        repo, ["symbolic-ref", "--quiet", "--short", "HEAD"], allow_fail=True
    )
    return branch or "HEAD"


def detect_base(repo: Path, target: str) -> str:
    for candidate in ("main", "master", "develop"):
        if candidate != target and ref_exists(repo, candidate):
            return candidate

    origin_head = text_git(
        repo,
        ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
        allow_fail=True,
    )
    if origin_head and origin_head != target and ref_exists(repo, origin_head):
        return origin_head

    raise GitError(
        "Could not auto-detect a base branch. Pass one explicitly with --base."
    )


def safe_archive_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    posix = PurePosixPath(normalized)
    if posix.is_absolute() or ".." in posix.parts or normalized.startswith("/"):
        raise GitError(f"Unsafe repository path refused: {path!r}")
    if not normalized or normalized.endswith("/"):
        raise GitError(f"Invalid file path: {path!r}")
    return normalized


def parse_name_status(raw: bytes) -> list[dict]:
    tokens = raw.split(b"\0")
    if tokens and tokens[-1] == b"":
        tokens.pop()

    entries: list[dict] = []
    i = 0
    while i < len(tokens):
        status = os.fsdecode(tokens[i])
        i += 1
        if not status:
            continue

        code = status[0]
        score = status[1:] or None

        if code in {"R", "C"}:
            if i + 1 >= len(tokens):
                raise GitError("Unexpected truncated rename/copy record from git diff.")
            old_path = safe_archive_path(os.fsdecode(tokens[i]))
            new_path = safe_archive_path(os.fsdecode(tokens[i + 1]))
            i += 2
            entries.append(
                {
                    "status": code,
                    "status_raw": status,
                    "score": score,
                    "old_path": old_path,
                    "path": new_path,
                }
            )
        else:
            if i >= len(tokens):
                raise GitError("Unexpected truncated record from git diff.")
            path = safe_archive_path(os.fsdecode(tokens[i]))
            i += 1
            entries.append(
                {
                    "status": code,
                    "status_raw": status,
                    "score": score,
                    "old_path": None,
                    "path": path,
                }
            )
    return entries


def get_changed_entries(repo: Path, from_sha: str, target_sha: str) -> list[dict]:
    raw = run_git(
        repo,
        [
            "diff",
            "--name-status",
            "-z",
            "--find-renames",
            "--find-copies",
            f"{from_sha}..{target_sha}",
            "--",
        ],
    )
    return parse_name_status(raw)


def matches_any(path: str, patterns: Iterable[str]) -> bool:
    name = PurePosixPath(path).name
    return any(fnmatch.fnmatchcase(path, p) or fnmatch.fnmatchcase(name, p) for p in patterns)


def list_tree_files(repo: Path, target_sha: str) -> list[str]:
    raw = run_git(repo, ["ls-tree", "-r", "-z", "--name-only", target_sha])
    return [
        safe_archive_path(os.fsdecode(p))
        for p in raw.split(b"\0")
        if p
    ]


def object_type(repo: Path, target_sha: str, path: str) -> str | None:
    out = text_git(
        repo, ["cat-file", "-t", f"{target_sha}:{path}"], allow_fail=True
    )
    return out or None


def read_blob(repo: Path, target_sha: str, path: str) -> bytes:
    return run_git(repo, ["cat-file", "blob", f"{target_sha}:{path}"])


def zipinfo(path: str) -> ZipInfo:
    info = ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    return info


def sanitize_ref(ref: str) -> str:
    ref = re.sub(r"[^A-Za-z0-9._-]+", "-", ref.strip())
    return ref.strip("-") or "ref"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def build_zip(args: argparse.Namespace) -> dict:
    repo = find_repo_root(Path(args.repo))
    target_ref = args.target or current_target(repo)
    base_ref = args.base or detect_base(repo, target_ref)

    target_sha = resolve_commit(repo, target_ref)
    base_sha = resolve_commit(repo, base_ref)

    if args.mode == "merge-base":
        compare_from_sha = text_git(repo, ["merge-base", base_sha, target_sha])
    else:
        compare_from_sha = base_sha

    entries = get_changed_entries(repo, compare_from_sha, target_sha)

    include_patterns = args.include or []
    exclude_patterns = args.exclude or []

    selected: set[str] = set()
    deleted: list[str] = []
    filtered_out: list[str] = []

    for entry in entries:
        path = entry["path"]
        if entry["status"] == "D":
            deleted.append(path)
            continue
        if include_patterns and not matches_any(path, include_patterns):
            filtered_out.append(path)
            continue
        if exclude_patterns and matches_any(path, exclude_patterns):
            filtered_out.append(path)
            continue
        selected.add(path)

    context_files: list[str] = []
    if args.scan_context:
        for path in list_tree_files(repo, target_sha):
            if path in selected:
                continue
            if matches_any(path, CONTEXT_PATTERNS):
                if exclude_patterns and matches_any(path, exclude_patterns):
                    continue
                context_files.append(path)
                selected.add(path)

    if args.fail_on_empty and not selected:
        raise GitError("No files selected for the ZIP.")

    out = Path(args.out) if args.out else (
        Path.cwd()
        / "dist"
        / (
            f"diffscan-{sanitize_ref(target_ref)}-vs-{sanitize_ref(base_ref)}-"
            f"{target_sha[:8]}.zip"
        )
    )
    out = out.expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    archived_files: list[str] = []
    skipped_non_blobs: list[dict] = []

    manifest = {
        "tool": "git-diff-scan-zip",
        "version": VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "repository_root": str(repo),
        "base_ref": base_ref,
        "base_sha": base_sha,
        "target_ref": target_ref,
        "target_sha": target_sha,
        "mode": args.mode,
        "compare_from_sha": compare_from_sha,
        "changed_entries": entries,
        "deleted_files": sorted(deleted),
        "filtered_out": sorted(set(filtered_out)),
        "scan_context_enabled": bool(args.scan_context),
        "context_files": sorted(context_files),
        "include_patterns": include_patterns,
        "exclude_patterns": exclude_patterns,
    }

    with ZipFile(out, "w", compression=ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(selected):
            typ = object_type(repo, target_sha, path)
            if typ != "blob":
                skipped_non_blobs.append({"path": path, "git_object_type": typ})
                continue
            data = read_blob(repo, target_sha, path)
            zf.writestr(zipinfo(path), data)
            archived_files.append(path)

        manifest["archived_files"] = archived_files
        manifest["skipped_non_blobs"] = skipped_non_blobs

        if not args.no_metadata:
            zf.writestr(
                zipinfo(f"{META_DIR}/manifest.json"),
                json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
            )
            status_lines = []
            for entry in entries:
                if entry["old_path"]:
                    status_lines.append(
                        f"{entry['status_raw']}\t{entry['old_path']}\t{entry['path']}"
                    )
                else:
                    status_lines.append(f"{entry['status_raw']}\t{entry['path']}")
            zf.writestr(
                zipinfo(f"{META_DIR}/changed-files.txt"),
                ("\n".join(status_lines) + ("\n" if status_lines else "")).encode("utf-8"),
            )

            if args.include_patch:
                patch = run_git(
                    repo,
                    [
                        "diff",
                        "--binary",
                        "--full-index",
                        f"{compare_from_sha}..{target_sha}",
                        "--",
                    ],
                )
                zf.writestr(zipinfo(f"{META_DIR}/changes.patch"), patch)

    result = {
        "zip": str(out),
        "sha256": sha256_file(out),
        "base_ref": base_ref,
        "target_ref": target_ref,
        "mode": args.mode,
        "changed_count": len(entries),
        "archived_count": len(archived_files),
        "deleted_count": len(deleted),
        "context_count": len(context_files),
        "skipped_non_blob_count": len(skipped_non_blobs),
    }
    return result


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="diffzip",
        description=(
            "ZIP files changed on a local Git branch while preserving repository paths."
        ),
    )
    p.add_argument(
        "repo",
        nargs="?",
        default=".",
        help="Path inside the local Git repository (default: current directory).",
    )
    p.add_argument(
        "--base",
        help="Base branch/ref. Auto-detects main/master/develop/origin HEAD if omitted.",
    )
    p.add_argument(
        "--target",
        help="Target local branch/ref (default: current branch/HEAD).",
    )
    p.add_argument(
        "--mode",
        choices=("merge-base", "direct"),
        default="merge-base",
        help=(
            "merge-base: files introduced/changed by target since divergence (default); "
            "direct: exact base tip vs target tip."
        ),
    )
    p.add_argument(
        "-o",
        "--out",
        help="Output ZIP path. Default: ./dist/diffscan-<target>-vs-<base>-<sha>.zip",
    )
    p.add_argument(
        "--include",
        action="append",
        metavar="GLOB",
        help="Only include changed paths matching this glob. Repeatable.",
    )
    p.add_argument(
        "--exclude",
        action="append",
        metavar="GLOB",
        help="Exclude paths matching this glob. Repeatable.",
    )
    p.add_argument(
        "--scan-context",
        action="store_true",
        help=(
            "Also include common build/scanner descriptors such as pom.xml, package.json, "
            "*.csproj, pyproject.toml and sonar-project.properties."
        ),
    )
    p.add_argument(
        "--include-patch",
        action="store_true",
        help=f"Include the full binary-capable diff as {META_DIR}/changes.patch.",
    )
    p.add_argument(
        "--no-metadata",
        action="store_true",
        help=f"Do not add {META_DIR}/manifest.json or changed-files.txt to the ZIP.",
    )
    p.add_argument(
        "--fail-on-empty",
        action="store_true",
        help="Exit non-zero if no source files are selected.",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable result JSON.",
    )
    p.add_argument("--version", action="version", version=VERSION)
    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        result = build_zip(args)
    except (GitError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"Created:  {result['zip']}")
        print(f"SHA-256:  {result['sha256']}")
        print(
            "Files:    "
            f"{result['archived_count']} archived, "
            f"{result['deleted_count']} deleted, "
            f"{result['context_count']} context"
        )
        print(
            f"Compare:  {result['base_ref']} -> {result['target_ref']} "
            f"({result['mode']})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
