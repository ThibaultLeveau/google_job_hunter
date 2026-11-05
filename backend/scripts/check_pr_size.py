import argparse
import json
import os
import subprocess
from dataclasses import dataclass

LOCKFILES = {"backend/uv.lock", "frontend/package-lock.json"}
MAX_FILES = 10
MAX_LINES = 1000


@dataclass(frozen=True)
class Change:
    additions: int
    deletions: int
    paths: tuple[str, ...]

    @property
    def lockfile(self) -> bool:
        # Renaming source into a lockfile must not exempt its authored changes.
        return all(path in LOCKFILES for path in self.paths)


def parse_numstat(data: bytes) -> list[Change]:
    if not data:
        return []
    if not data.endswith(b"\0"):
        raise ValueError("Unterminated numstat output")
    records = iter(data[:-1].split(b"\0"))
    changes = []
    try:
        for record in records:
            added, deleted, path = record.split(b"\t", 2)
            paths = (path,) if path else (next(records), next(records))
            if not added.isdigit() or not deleted.isdigit() or not all(paths):
                raise ValueError("Invalid numstat record")
            changes.append(
                Change(int(added), int(deleted), tuple(os.fsdecode(p) for p in paths))
            )
    except StopIteration as error:
        raise ValueError("Incomplete rename record") from error
    return changes


def git(*arguments: str) -> bytes:
    return subprocess.run(["git", *arguments], check=True, capture_output=True).stdout


def branch_changes(base: str, head: str) -> list[Change]:
    commits = [
        git("rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
        .decode("ascii")
        .strip()
        for ref in (base, head)
    ]
    merge_base = git("merge-base", *commits).decode("ascii").strip()
    return parse_numstat(
        git(
            "diff",
            "--numstat",
            "-z",
            "--find-renames",
            "--no-ext-diff",
            "--no-textconv",
            "--text",
            merge_base,
            commits[1],
            "--",
        )
    )


def report(changes: list[Change]) -> int:
    raw_added = sum(change.additions for change in changes)
    raw_deleted = sum(change.deletions for change in changes)
    lock_added = sum(change.additions for change in changes if change.lockfile)
    lock_deleted = sum(change.deletions for change in changes if change.lockfile)
    authored_added, authored_deleted = (
        raw_added - lock_added,
        raw_deleted - lock_deleted,
    )
    for change in changes:
        # JSON escaping keeps filenames from becoming shell or workflow commands.
        print(
            json.dumps(
                {
                    "paths": change.paths,
                    "additions": change.additions,
                    "deletions": change.deletions,
                    "dependency_lockfile": change.lockfile,
                }
            )
        )
    print(f"Files: {len(changes)} / {MAX_FILES} (renames count once; locks count)")
    print(f"Raw: +{raw_added}/-{raw_deleted} = {raw_added + raw_deleted}")
    print(
        f"Dependency locks: +{lock_added}/-{lock_deleted} = {lock_added + lock_deleted}"
    )
    authored = authored_added + authored_deleted
    print(f"Authored: +{authored_added}/-{authored_deleted} = {authored} / {MAX_LINES}")
    failures = []
    if len(changes) > MAX_FILES:
        failures.append(f"File cap exceeded: {len(changes)} > {MAX_FILES}")
    if authored > MAX_LINES:
        failures.append(f"Authored-line cap exceeded: {authored} > {MAX_LINES}")
    for failure in failures:
        print(f"FAIL: {failure}")
    return int(bool(failures))


def main() -> int:
    parser = argparse.ArgumentParser(description="Check a PR's merge-base diff size.")
    parser.add_argument("base")
    parser.add_argument("head")
    args = parser.parse_args()
    try:
        return report(branch_changes(args.base, args.head))
    except ValueError:
        print(
            "FAIL: Uncountable diff (binary or malformed numstat); size not approved."
        )
        return 2
    except OSError, subprocess.CalledProcessError:
        print(
            "FAIL: Could not measure diff; fetch complete base/head history and retry."
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
