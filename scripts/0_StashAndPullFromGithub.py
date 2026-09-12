#!/usr/bin/env python3
"""
0_StashAndPullFromGithub.py

Syncs this working copy to the latest commit on whatever branch is
currently checked out, before a pipeline run: stashes any local
uncommitted changes to tracked files (if there are any), pulls, then
restores the stash.

Safe by design -- nothing here ever force-discards real local work:
    - If there's nothing to stash, it just pulls.
    - If restoring the stash (git stash pop) would conflict with what was
      just pulled, this STOPS and tells you exactly that, rather than
      silently leaving your working tree half-merged or dropping the
      stash. Your changes stay safe in the stash either way -- `git stash
      list` / `git stash show -p` to look at them, resolve by hand.

NOTE: this is a separate Python process, so it can't change your shell's
current directory the way a shell `cd` would -- if you're running this
from the repo root rather than from inside scripts/, you'll still need to
`cd scripts` yourself afterward.

Run:   python3 0_StashAndPullFromGithub.py
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def run(args, check=True):
    result = subprocess.run(args, cwd=HERE, capture_output=True, text=True)
    if check and result.returncode != 0:
        print(f"failed: {' '.join(args)}")
        if result.stdout.strip():
            print(result.stdout.strip())
        if result.stderr.strip():
            print(result.stderr.strip())
        sys.exit(1)
    return result


def main():
    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    print(f"on branch {branch!r}")

    status = run(["git", "status", "--porcelain"]).stdout
    has_local_changes = bool(status.strip())

    if has_local_changes:
        print("local changes found -- stashing before pulling:")
        print(status.strip())
        # -u (--include-untracked) matters: plain `git stash` only stashes
        # tracked changes. An untracked file (e.g. a new script sitting on
        # disk that was never `git add`ed) wouldn't be touched by a plain
        # stash, then blocks the pull outright if the incoming commit tries
        # to create a tracked file at that same path -- "would be
        # overwritten by merge." -u covers that case too.
        stash = run(["git", "stash", "-u"])
        print(stash.stdout.strip())
    else:
        print("no local changes -- nothing to stash")

    print(f"pulling origin/{branch}...")
    pull = run(["git", "pull", "origin", branch])
    print(pull.stdout.strip())

    if has_local_changes:
        print("restoring stashed changes...")
        pop = run(["git", "stash", "pop"], check=False)
        if pop.stdout.strip():
            print(pop.stdout.strip())
        if pop.returncode != 0:
            if pop.stderr.strip():
                print(pop.stderr.strip())
            print(
                "\nSTOPPED: restoring your stashed changes conflicted with what was "
                "just pulled. Nothing was discarded -- your changes are still safe "
                "in the stash (`git stash list` to see them, `git stash show -p` to "
                "see what's in it). Resolve by hand: fix the conflict and "
                "`git stash pop` again, or `git stash drop` if the stashed version "
                "turns out to be redundant with what you just pulled (which is what "
                "it's usually been so far in this project)."
            )
            sys.exit(1)

    print("done.")


if __name__ == "__main__":
    main()
