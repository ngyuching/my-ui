#!/usr/bin/env python3

import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from zipfile import ZipFile

MODULE_PATH = Path(__file__).resolve().parents[1] / "diffzip.py"
spec = importlib.util.spec_from_file_location("diffzip", MODULE_PATH)
diffzip = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(diffzip)


def git(repo: Path, *args: str) -> str:
    p = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return p.stdout.strip()


class DiffZipTests(unittest.TestCase):
    def init_repo(self, root: Path) -> Path:
        repo = root / "repo"
        repo.mkdir()
        git(repo, "init", "-q")
        git(repo, "config", "user.email", "test@example.com")
        git(repo, "config", "user.name", "Test User")
        (repo / "src").mkdir()
        (repo / "src" / "a.txt").write_text("a\n", encoding="utf-8")
        (repo / "pom.xml").write_text("<project/>\n", encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "initial")
        git(repo, "branch", "-M", "main")
        return repo

    def test_modified_added_deleted_and_context(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self.init_repo(root)
            git(repo, "checkout", "-qb", "feature")
            (repo / "src" / "a.txt").write_text("changed\n", encoding="utf-8")
            (repo / "src" / "b.txt").write_text("new\n", encoding="utf-8")
            git(repo, "rm", "-q", "pom.xml")
            (repo / "package.json").write_text('{"name":"x"}\n', encoding="utf-8")
            git(repo, "add", ".")
            git(repo, "commit", "-qm", "feature work")

            args = diffzip.build_parser().parse_args(
                [
                    str(repo),
                    "--base", "main",
                    "--target", "feature",
                    "--scan-context",
                    "-o", str(root / "out.zip"),
                ]
            )
            result = diffzip.build_zip(args)

            self.assertEqual(result["deleted_count"], 1)
            with ZipFile(root / "out.zip") as zf:
                names = set(zf.namelist())
                self.assertIn("src/a.txt", names)
                self.assertIn("src/b.txt", names)
                self.assertIn("package.json", names)
                self.assertNotIn("pom.xml", names)
                self.assertIn(".diffscan/manifest.json", names)

    def test_rename_uses_new_path(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self.init_repo(root)
            git(repo, "checkout", "-qb", "rename")
            git(repo, "mv", "src/a.txt", "src/renamed.txt")
            git(repo, "commit", "-qm", "rename")

            args = diffzip.build_parser().parse_args(
                [
                    str(repo),
                    "--base", "main",
                    "--target", "rename",
                    "-o", str(root / "rename.zip"),
                ]
            )
            diffzip.build_zip(args)

            with ZipFile(root / "rename.zip") as zf:
                names = set(zf.namelist())
                self.assertIn("src/renamed.txt", names)
                self.assertNotIn("src/a.txt", names)

    def test_merge_base_does_not_pull_base_only_changes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self.init_repo(root)
            git(repo, "checkout", "-qb", "feature")
            (repo / "src" / "feature.txt").write_text("feature\n", encoding="utf-8")
            git(repo, "add", ".")
            git(repo, "commit", "-qm", "feature commit")

            git(repo, "checkout", "main")
            (repo / "src" / "main-only.txt").write_text("main\n", encoding="utf-8")
            git(repo, "add", ".")
            git(repo, "commit", "-qm", "main commit")

            args = diffzip.build_parser().parse_args(
                [
                    str(repo),
                    "--base", "main",
                    "--target", "feature",
                    "-o", str(root / "mb.zip"),
                ]
            )
            diffzip.build_zip(args)

            with ZipFile(root / "mb.zip") as zf:
                names = set(zf.namelist())
                self.assertIn("src/feature.txt", names)
                self.assertNotIn("src/main-only.txt", names)


if __name__ == "__main__":
    unittest.main()
