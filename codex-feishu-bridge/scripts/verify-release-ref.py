"""Verify a published Yinshi release using only the checkout and its Git tags.

No network, credentials, host database, CLI invocation or service changes.
"""

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import tomllib


def verify(repo: Path, channel: str) -> dict[str, str]:
    spec = json.loads((repo / "deploy" / f"{channel}.json").read_text())
    tag = spec["tag"]
    match = re.fullmatch(r"bridge-v(\d+\.\d+\.\d+)(?:-rc\.(\d+))?", tag)
    if not match:
        raise ValueError(f"invalid release tag in {channel}")
    version = match[1] + ("rc" + match[2] if match[2] else "")
    if spec["bridge_version"] != version:
        raise ValueError(f"{channel}: release tag and package version disagree")

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True)

    sha = git("rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}").strip()
    if (channel == "candidate" or "sha" in spec) and spec.get("sha") != sha:
        raise ValueError(f"{channel}: published tag does not match the pinned commit")
    release = json.loads(git("show", f"{sha}:deploy/stable.json"))
    package = tomllib.loads(git("show", f"{sha}:codex-feishu-bridge/pyproject.toml"))
    if any(release[k] != spec[k] for k in ("tag", "bridge_version", "codex_version")):
        raise ValueError(f"{channel}: manifest differs from the tagged release")
    if package["project"]["version"] != version:
        raise ValueError(f"{channel}: tagged Python package version does not match")
    return {
        "channel": channel,
        "tag": tag,
        "sha": sha,
        "bridge_version": version,
        "codex_version": spec["codex_version"],
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    p.add_argument("--channel", choices=("stable", "candidate"), required=True)
    args = p.parse_args()
    try:
        print(json.dumps(verify(args.repo, args.channel)))
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        sys.exit(f"Release ref verification failed: {exc}")
