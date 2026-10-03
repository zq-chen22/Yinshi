#!/usr/bin/env python3
"""Build a checked, state-free release kit for same-platform SSH deployments."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import zipfile


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--repo", type=Path, required=True)
    p.add_argument("--tag", required=True)
    p.add_argument("--wheelhouse", type=Path, required=True)
    p.add_argument("--cli-package", type=Path, required=True)
    p.add_argument("--output-root", type=Path, required=True)
    args = p.parse_args()
    git = ["git", "-C", str(args.repo)]
    sha = subprocess.check_output(git + ["rev-parse", args.tag + "^{commit}"], text=True).strip()
    channel = json.loads(subprocess.check_output(git + ["show", f"{sha}:deploy/stable.json"], text=True))
    if channel["tag"] != args.tag:
        raise ValueError("tag differs from the promoted channel")
    version = subprocess.check_output([str(args.cli_package / "bin/codex"), "--version"], text=True).strip().split()[-1]
    if version != channel["codex_version"]:
        raise ValueError("CLI package version differs from release")
    if not (args.cli_package / "codex-package.json").is_file():
        raise ValueError("not a standalone Codex package; never pass CODEX_HOME")
    wheels = list(args.wheelhouse.glob(f"codex_feishu_bridge-{channel['bridge_version']}-*.whl"))
    if len(wheels) != 1:
        raise ValueError("exactly one matching bridge wheel is required")
    tracked = subprocess.check_output(git + ["ls-tree", "-r", "--name-only", sha], text=True).splitlines()
    prefix = "codex-feishu-bridge/src/"
    with zipfile.ZipFile(wheels[0]) as wheel:
        for name in tracked:
            if name.startswith(prefix) and (name.endswith(".py") or name.endswith("config.example.toml")):
                committed = subprocess.check_output(git + ["show", f"{sha}:{name}"])
                if wheel.read(name[len(prefix):]) != committed:
                    raise ValueError("wheel differs from committed source: " + name)
    output = args.output_root / sha
    if output.exists():
        raise ValueError("artifact already exists; immutable releases must not be overwritten")
    output.mkdir(parents=True, mode=0o700)
    branch = subprocess.check_output(git + ["symbolic-ref", "--short", "HEAD"], text=True).strip()
    subprocess.run(git + ["bundle", "create", str(output / "source.bundle"), args.tag, branch], check=True)
    shutil.copytree(args.wheelhouse, output / "wheelhouse")
    shutil.copytree(args.cli_package, output / "codex")
    checksums = {
        str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(output.rglob("*")) if path.is_file()
    }
    (output / "checksums.json").write_text(json.dumps(checksums, indent=2) + "\n")
    print(json.dumps({"sha": sha, "tag": args.tag, "files": len(checksums), "artifact": str(output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
