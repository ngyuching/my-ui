# git-diff-scan-zip

Export only the files changed by a local Git branch into a ZIP while preserving the repository directory structure.

Useful when you want to prepare a focused source bundle for tools such as:

- Checkmarx
- SonarQube / SonarScanner staging
- other SAST/static-analysis workflows that accept or work better with a reduced source tree

> Important: SonarQube normally works best when scanning a normal checkout with the project's build context. A diff-only ZIP is mainly useful as a staging/export helper. Use `--scan-context` to also include common project/build descriptor files.

## Features

- compares a local target branch against a base branch
- defaults to merge-base comparison, which is normally what you want for feature branches
- preserves original repository paths inside the ZIP
- handles added, modified, renamed and copied files
- deleted files are recorded in metadata but are not added to the archive
- optional common scanner/build context files
- optional unified binary-capable patch
- JSON output for CI scripting
- no third-party Python dependencies

## Requirements

- Git
- Python 3.9+

## Quick start

From anywhere inside your repository:

```bash
python tools/git-diff-scan-zip/diffzip.py --base main --target my-feature
```

Example output:

```text
dist/diffscan-my-feature-vs-main-a1b2c3d4.zip
```

The ZIP contains the changed files with their original paths, for example:

```text
src/api/UserController.java
src/service/UserService.java
pom.xml
.diffscan/manifest.json
.diffscan/changed-files.txt
```

## Recommended usage for Checkmarx

```bash
python tools/git-diff-scan-zip/diffzip.py \
  --base main \
  --target feature/login-fix \
  --scan-context \
  -o checkmarx-source.zip
```

Upload `checkmarx-source.zip` to the relevant Checkmarx scan flow.

## Recommended usage for SonarQube

SonarQube usually expects a project checkout and may require build metadata, generated classpath information, dependencies, or compiled output depending on the language.

For a reduced source bundle:

```bash
python tools/git-diff-scan-zip/diffzip.py \
  --base main \
  --target feature/login-fix \
  --scan-context \
  -o sonar-source.zip
```

Then extract the ZIP and run your normal scanner from that extracted directory.

For Java/C#/TypeScript and other build-aware analyzers, a full checkout with scanner inclusion filters may still produce better results than a source-only diff archive.

## Comparison modes

### merge-base (default)

```text
merge-base(base, target) .. target
```

This exports changes introduced on the target branch since it diverged from the base branch.

```bash
python diffzip.py --base main --target feature/my-work
```

### direct

```text
base-tip .. target-tip
```

Use this when you explicitly want the exact current snapshots compared.

```bash
python diffzip.py --base main --target feature/my-work --mode direct
```

## Include scanner/build context

```bash
python diffzip.py --base main --target feature/my-work --scan-context
```

This also includes common files such as:

- `sonar-project.properties`
- `pom.xml`
- Gradle files
- `package.json` and common JS lock files
- `pyproject.toml` / Python dependency files
- `*.sln` / `*.csproj`
- `go.mod`
- `Cargo.toml`

## Include or exclude paths

Only Java and XML:

```bash
python diffzip.py \
  --base main \
  --target feature/my-work \
  --include "*.java" \
  --include "*.xml"
```

Exclude tests and generated files:

```bash
python diffzip.py \
  --base main \
  --target feature/my-work \
  --exclude "*/test/*" \
  --exclude "dist/*" \
  --exclude "node_modules/*"
```

## Include the patch

```bash
python diffzip.py --base main --target feature/my-work --include-patch
```

The archive will contain:

```text
.diffscan/changes.patch
```

## CI / scripting output

```bash
python diffzip.py --base main --target feature/my-work --json
```

Example:

```json
{
  "zip": "/path/to/dist/diffscan-feature-my-work-vs-main-a1b2c3d4.zip",
  "sha256": "...",
  "base_ref": "main",
  "target_ref": "feature/my-work",
  "mode": "merge-base",
  "changed_count": 5,
  "archived_count": 4,
  "deleted_count": 1,
  "context_count": 1,
  "skipped_non_blob_count": 0
}
```

## Metadata

By default, every ZIP contains:

### `.diffscan/manifest.json`

Records:

- base and target refs
- resolved commit SHAs
- merge-base used for comparison
- changed file statuses
- deleted files
- context files
- included/excluded patterns
- files actually archived

### `.diffscan/changed-files.txt`

A compact Git-style changed file list.

Disable metadata with:

```bash
python diffzip.py --no-metadata
```

## Notes

- Files are read from the target Git commit, not from uncommitted working-tree changes.
- Submodules are not recursively packaged; non-blob Git objects are listed as skipped in the manifest.
- Symlink entries stored as Git blobs are archived as their Git blob content.
- The ZIP is intended to be deterministic for source contents, though the metadata generation timestamp changes per run.
