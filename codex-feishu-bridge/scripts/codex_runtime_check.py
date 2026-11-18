from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import struct
import subprocess
import tempfile
import tomllib


def package_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ValueError("invalid runtime package path")
    parts = PurePosixPath(relative)
    if parts.is_absolute() or ".." in parts.parts:
        raise ValueError("runtime package path escapes package")
    result = root / relative
    if not result.resolve().is_relative_to(root.resolve()):
        raise ValueError("runtime package symlink escapes package")
    return result


def package_layout(root: Path, expected_version: str | None = None) -> tuple[dict, dict[str, Path]]:
    manifest_path = package_path(root, "codex-package.json")
    manifest = json.loads(manifest_path.read_text())
    if type(manifest.get("layoutVersion")) is not int or manifest["layoutVersion"] != 1:
        raise ValueError("unsupported standalone layout; update the deployment validator first")
    if manifest.get("variant") != "codex" or manifest.get("entrypoint") != "bin/codex":
        raise ValueError("expected a full Codex CLI package")
    if not isinstance(manifest.get("version"), str) or not manifest["version"]:
        raise ValueError("runtime package version is missing")
    if expected_version is not None and manifest["version"] != expected_version:
        raise ValueError("runtime package version differs from release")
    if not isinstance(manifest.get("target"), str) or "linux" not in manifest["target"]:
        raise ValueError("this systemd fleet validator requires a Linux runtime package")
    resources = package_path(root, manifest.get("resourcesDir", ""))
    command_path = package_path(root, manifest.get("pathDir", ""))
    required = {
        "cli": package_path(root, manifest["entrypoint"]),
        "code_mode_host": package_path(root, "bin/codex-code-mode-host"),
        "sandbox_helper": resources / "bwrap",
        "ripgrep": command_path / "rg",
    }
    for name, path in required.items():
        package_path(root, path.relative_to(root).as_posix())
        if not path.is_file() or not os.access(path, os.X_OK) or path.stat().st_size == 0:
            raise ValueError("missing or non-executable runtime component: " + name)
    return manifest, required


def fingerprint(root: Path) -> dict[str, str]:
    files = {}
    for path in sorted(root.rglob("*")):
        package_path(root, path.relative_to(root).as_posix())
        if path.is_file():
            checksum = hashlib.sha256()
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    checksum.update(block)
            files[path.relative_to(root).as_posix()] = checksum.hexdigest()
    return files


async def host_probe(host: Path, environment: dict[str, str]) -> None:
    process = await asyncio.create_subprocess_exec(str(host), "--listen", "stdio://", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=environment)

    async def send(value: dict) -> None:
        payload = json.dumps(value).encode()
        process.stdin.write(struct.pack("<I", len(payload)) + payload)
        await process.stdin.drain()

    async def receive() -> dict:
        length = struct.unpack("<I", await process.stdout.readexactly(4))[0]
        if not 0 < length <= 1024 * 1024:
            raise ValueError("invalid code-mode IPC frame")
        return json.loads(await process.stdout.readexactly(length))

    try:
        await send({"type": "connection/hello", "supportedVersions": [1], "requiredCapabilities": [], "optionalCapabilities": []})
        hello = await receive()
        if hello.get("type") != "connection/ready" or hello.get("selectedVersion") != 1:
            raise ValueError("code-mode IPC handshake failed")
        await send({"type": "operation/request", "id": 1, "request": {"method": "session/open", "sessionId": "runtime-smoke"}})
        opened = await receive()
        if opened.get("result", {}).get("value", {}).get("type") != "session/ready":
            raise ValueError("code-mode session open failed")
        nonce = "yinshi-runtime-tool-probe"
        tool = {"name": "runtime_probe", "tool_name": {"name": "runtime_probe", "namespace": None}, "description": "Offline deployment self-test", "kind": "function", "input_schema": {"type": "object", "properties": {"nonce": {"type": "string"}}, "required": ["nonce"], "additionalProperties": False}, "output_schema": None}
        await send({"type": "operation/request", "id": 2, "request": {"method": "session/execute", "sessionId": "runtime-smoke", "request": {"tool_call_id": "offline-deployment-smoke", "enabled_tools": [tool], "source": 'text(6 * 7); text((await tools.runtime_probe({nonce: "' + nonce + '"})).value);', "yield_time_ms": 10000, "max_output_tokens": 100}}})
        callbacks = 0
        for frame_index in range(16):
            frame = await receive()
            if frame.get("type") == "delegate/request":
                request = frame.get("request", {})
                invocation = request.get("invocation", {})
                if request.get("type") != "tool/invoke" or invocation.get("tool_name") != tool["tool_name"] or invocation.get("input") != {"nonce": nonce}:
                    raise ValueError("unexpected code-mode probe delegate")
                callbacks += 1
                await send({"type": "delegate/response", "id": frame["id"], "result": {"status": "ok", "value": {"type": "tool/result", "result": {"value": nonce}}}})
            elif frame.get("type") == "execute/initialResponse":
                result = frame.get("result", {})
                output = result.get("value", {}).get("Result", {})
                text = [item.get("text") for item in output.get("content_items", []) if item.get("type") == "input_text"]
                if result.get("status") != "ok" or output.get("error_text") or callbacks != 1 or text != ["42", nonce]:
                    raise ValueError("code-mode JavaScript/tool roundtrip failed")
                return
            elif frame.get("result", {}).get("status") == "error":
                raise ValueError("code-mode execution operation failed")
        raise ValueError("code-mode probe returned too many frames")
    finally:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 2)
            except TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()


def check_package(root: Path, expected_version: str | None = None, *, probe: bool = True, hashes: bool = True) -> dict:
    root = root.resolve()
    manifest, required = package_layout(root, expected_version)
    if probe:
        with tempfile.TemporaryDirectory(prefix="codex-runtime-smoke-") as temporary:
            environment = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": temporary, "CODEX_HOME": str(Path(temporary) / "codex")}
            Path(environment["CODEX_HOME"]).mkdir()
            version = subprocess.check_output([str(required["cli"]), "--version"], env=environment, stderr=subprocess.PIPE, text=True, timeout=10).strip().split()[-1]
            if version != manifest["version"]:
                raise ValueError("CLI binary version differs from its package manifest")
            asyncio.run(asyncio.wait_for(host_probe(required["code_mode_host"], environment), 15))
    return {"status": "PASS", "version": manifest["version"], "target": manifest["target"], "offline_tool_roundtrip": probe, "files": fingerprint(root) if hashes else {}}


def main() -> int:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--package", type=Path)
    source.add_argument("--config", type=Path)
    parser.add_argument("--version")
    parser.add_argument("--files-only", action="store_true")
    parser.add_argument("--hashes", action="store_true")
    parser.add_argument("--expected-files", type=Path)
    parser.add_argument("--condition", action="store_true")
    args = parser.parse_args()
    try:
        if args.config:
            config = tomllib.loads(args.config.read_text())
            binary = Path(config["bridge"]["codex_bin"]).expanduser()
            if not binary.is_absolute():
                raise ValueError("production codex_bin must pin an absolute runtime path")
            args.package = binary.resolve().parent.parent
        result = check_package(args.package, args.version, probe=not args.files_only, hashes=args.hashes or args.expected_files is not None)
        if args.expected_files and result["files"] != json.loads(args.expected_files.read_text())["files"]:
            raise ValueError("runtime package differs from the verified complete package")
    except Exception as error:
        if not args.condition:
            raise
        print(json.dumps({"status": "FAIL", "reason": f"{type(error).__name__}: {str(error)[:240]}"}))
        return 1
    if not args.hashes:
        result.pop("files")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

