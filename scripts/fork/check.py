"""Check the fork's integration branch against its manifest, or draft notes.

``fork/branches.txt`` lists, in merge order, the branches the integration
branch is rebuilt from: upstream ``main``, then a no-fast-forward merge of
each. ``check`` verifies that a rebuild matches the manifest. ``notes``
drafts release notes for a tag: the upstream commit it is based on, and each
branch with the tip merged and the state of its upstream pull request.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

UPSTREAM = "stickerdaniel/linkedin-mcp-server"
MANIFEST = "fork/branches.txt"
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_MERGE_SUBJECT = re.compile(
    r"^Merge (?:branch '|remote-tracking branch '[^/']+/)(?P<branch>[^']+)'"
)


@dataclass(frozen=True)
class Entry:
    branch: str
    pr: int | None


@dataclass(frozen=True)
class Merge:
    commit: str
    branch: str | None
    tip: str


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True, cwd=_REPO_ROOT
    ).stdout.strip()


def git_ok(*args: str) -> bool:
    return (
        subprocess.run(
            ["git", *args], capture_output=True, text=True, cwd=_REPO_ROOT
        ).returncode
        == 0
    )


def parse_manifest(text: str) -> list[Entry]:
    entries = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) != 2 or not (fields[1] == "-" or fields[1].isdigit()):
            raise SystemExit(f"{MANIFEST}:{number}: expected '<branch> <pr|->'")
        pr = None if fields[1] == "-" else int(fields[1])
        entries.append(Entry(fields[0], pr))
    return entries


def resolve(branch: str) -> str | None:
    """A branch's commit: the local branch, else the first remote holding it."""
    if git_ok("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"):
        return git("rev-parse", f"refs/heads/{branch}")
    for ref in git(
        "for-each-ref", "--format=%(refname)", f"refs/remotes/*/{branch}"
    ).splitlines():
        return git("rev-parse", ref)
    return None


def first_parent_line(integration: str) -> tuple[list[Merge], str]:
    """The merges above the rebuild's base, newest first, and the base.

    The base is the first commit down the first-parent line that is not a
    merge: ``main`` as it was when the branch was reset to it.
    """
    merges = []
    for line in git(
        "log", "--first-parent", "--format=%H %P%x09%s", integration
    ).splitlines():
        hashes, subject = line.split("\t", 1)
        commit, *parents = hashes.split()
        if len(parents) < 2:
            return merges, commit
        match = _MERGE_SUBJECT.match(subject)
        merges.append(Merge(commit, match["branch"] if match else None, parents[1]))
    raise SystemExit(f"{integration} has no first-parent root")


def check(integration: str, main: str, entries: list[Entry]) -> list[str]:
    problems = []
    names = {entry.branch for entry in entries}
    for entry in entries:
        commit = resolve(entry.branch)
        if commit is None:
            problems.append(f"{entry.branch}: no such branch")
        elif not git_ok("merge-base", "--is-ancestor", commit, integration):
            problems.append(f"{entry.branch}: tip {commit[:8]} is not in {integration}")
    merges, base = first_parent_line(integration)
    for merge in merges:
        if merge.branch not in names:
            label = merge.branch or git("log", "-1", "--format=%s", merge.commit)
            problems.append(
                f"merge {merge.commit[:8]} ({label}) names no manifest branch"
            )
    if not git_ok("merge-base", "--is-ancestor", base, main):
        problems.append(
            f"first-parent root {base[:8]} is not {main} or an ancestor of it"
        )
    return problems


def pr_state(number: int) -> str:
    result = subprocess.run(
        ["gh", "pr", "view", str(number), "--repo", UPSTREAM, "--json", "state"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return "unknown"
    return str(json.loads(result.stdout)["state"]).lower()


def notes(tag: str, entries: list[Entry]) -> str:
    merges, base = first_parent_line(tag)
    tips: dict[str, str] = {}
    for merge in reversed(merges):
        if merge.branch is not None:
            tips[merge.branch] = merge.tip
    date = git("log", "-1", "--format=%cs", base)
    described = (
        git("describe", "--tags", "--match", "v4.*", base)
        if git_ok("describe", "--tags", "--match", "v4.*", base)
        else "no v4 tag"
    )
    lines = [
        f"Based on upstream `{base[:12]}` ({date}, `{described}`).",
        "",
        "| Branch | Tip | Upstream PR |",
        "| --- | --- | --- |",
    ]
    for entry in entries:
        tip = tips.get(entry.branch)
        if tip is None:
            # A branch a later one contains can come in through that later merge.
            commit = resolve(entry.branch)
            if commit is not None and git_ok(
                "merge-base", "--is-ancestor", commit, tag
            ):
                tip = commit
        tip_cell = f"`{tip[:8]}`" if tip else "not merged"
        if entry.pr is None:
            pr_cell = "-"
        else:
            pr_cell = f"[#{entry.pr}](https://github.com/{UPSTREAM}/pull/{entry.pr}) {pr_state(entry.pr)}"
        lines.append(f"| `{entry.branch}` | {tip_cell} | {pr_cell} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    check_parser = commands.add_parser("check", help="verify a rebuild")
    check_parser.add_argument("--branch", default="jobsearch")
    check_parser.add_argument("--main", default="main")
    check_parser.add_argument("--manifest", type=Path, default=_REPO_ROOT / MANIFEST)
    notes_parser = commands.add_parser("notes", help="draft a tag's release notes")
    notes_parser.add_argument("tag")
    args = parser.parse_args(argv)

    if args.command == "check":
        problems = check(
            args.branch, args.main, parse_manifest(args.manifest.read_text())
        )
        for problem in problems:
            print(problem)
        return 1 if problems else 0
    # The manifest the tag was built from, not the checkout's.
    print(notes(args.tag, parse_manifest(git("show", f"{args.tag}:{MANIFEST}"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
