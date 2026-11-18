from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


SCRIPTS = Path(__file__).parents[1] / "scripts"
SPEC = importlib.util.spec_from_file_location("codex_runtime_check", SCRIPTS / "codex_runtime_check.py")
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


HOST = '''
import json,struct,sys
def receive():
    length=struct.unpack('<I',sys.stdin.buffer.read(4))[0]
    return json.loads(sys.stdin.buffer.read(length))
def send(value):
    payload=json.dumps(value).encode()
    sys.stdout.buffer.write(struct.pack('<I',len(payload))+payload)
    sys.stdout.buffer.flush()
receive()
send({'type':'connection/ready','selectedVersion':1,'capabilities':[]})
receive()
send({'type':'operation/response','id':1,'result':{'status':'ok','value':{'type':'session/ready','sessionId':'runtime-smoke'}}})
receive()
send({'type':'delegate/request','id':9,'sessionId':'runtime-smoke','request':{'type':'tool/invoke','invocation':{'tool_name':{'name':'runtime_probe','namespace':None},'input':{'nonce':'yinshi-runtime-tool-probe'}}}})
response=receive()
nonce=response['result']['value']['result']['value']
send({'type':'execute/initialResponse','id':2,'result':{'status':'ok','value':{'Result':{'content_items':[{'type':'input_text','text':'42'},{'type':'input_text','text':nonce}],'error_text':None}}}})
'''


@pytest.fixture
def package(tmp_path: Path) -> Path:
    root = tmp_path / "package"
    for folder in ["bin", "codex-resources", "codex-path"]:
        (root / folder).mkdir(parents=True)
    manifest = {"layoutVersion": 1, "version": "0.164.0", "target": "x86_64-unknown-linux-musl", "variant": "codex", "entrypoint": "bin/codex", "resourcesDir": "codex-resources", "pathDir": "codex-path"}
    (root / "codex-package.json").write_text(json.dumps(manifest))
    for name in ["bin/codex", "codex-resources/bwrap", "codex-path/rg"]:
        (root / name).write_text("#!/bin/sh\necho codex-cli 0.164.0\n")
        (root / name).chmod(0o755)
    host = root / "bin/codex-code-mode-host"
    host.write_text("#!" + sys.executable + "\n" + HOST)
    host.chmod(0o755)
    return root


def test_complete_package_executes_offline_js_and_nested_tool(package: Path):
    result = runtime.check_package(package, "0.164.0")
    assert result["offline_tool_roundtrip"]
    assert len(result["files"]) == 5


def test_original_main_binary_plus_synthetic_manifest_is_rejected(package: Path):
    manifest = json.loads((package / "codex-package.json").read_text())
    manifest.pop("resourcesDir")
    manifest.pop("pathDir")
    (package / "codex-package.json").write_text(json.dumps(manifest))
    assert subprocess.check_output([str(package / "bin/codex"), "--version"], text=True).strip().endswith("0.164.0")
    with pytest.raises(ValueError):
        runtime.check_package(package)


@pytest.mark.parametrize("component", ["bin/codex", "bin/codex-code-mode-host", "codex-resources/bwrap", "codex-path/rg"])
def test_each_required_component_is_mandatory(package: Path, component: str):
    (package / component).unlink()
    with pytest.raises(ValueError, match="runtime component"):
        runtime.check_package(package, probe=False)


def test_non_executable_host_is_rejected(package: Path):
    (package / "bin/codex-code-mode-host").chmod(0o644)
    with pytest.raises(ValueError, match="non-executable"):
        runtime.check_package(package, probe=False)


def test_empty_host_is_rejected(package: Path):
    (package / "bin/codex-code-mode-host").write_bytes(b"")
    with pytest.raises(ValueError, match="runtime component"):
        runtime.check_package(package, probe=False)


def test_host_help_exit_zero_is_not_a_real_execution_probe(package: Path):
    (package / "bin/codex-code-mode-host").write_text("#!/bin/sh\nexit 0\n")
    assert subprocess.run([str(package / "bin/codex-code-mode-host"), "--help"]).returncode == 0
    with pytest.raises(Exception):
        runtime.check_package(package)


def test_host_wrong_js_result_is_rejected(package: Path):
    host = package / "bin/codex-code-mode-host"
    host.write_text(host.read_text().replace("'text':'42'", "'text':'43'"))
    with pytest.raises(ValueError, match="roundtrip"):
        runtime.check_package(package)


def test_host_wrong_nested_tool_is_rejected(package: Path):
    host = package / "bin/codex-code-mode-host"
    host.write_text(host.read_text().replace("'name':'runtime_probe'", "'name':'unexpected'"))
    with pytest.raises(ValueError, match="delegate"):
        runtime.check_package(package)


@pytest.mark.parametrize("field,value", [("layoutVersion", 2), ("variant", "app-server"), ("resourcesDir", "../outside"), ("pathDir", "/usr/bin"), ("target", "unknown")])
def test_invalid_or_unsupported_layout_is_rejected(package: Path, field: str, value):
    manifest = json.loads((package / "codex-package.json").read_text())
    manifest[field] = value
    (package / "codex-package.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        runtime.check_package(package, probe=False)


def test_package_version_must_match_release(package: Path):
    with pytest.raises(ValueError, match="version differs"):
        runtime.check_package(package, "0.999.0")


def test_binary_version_must_match_manifest(package: Path):
    (package / "bin/codex").write_text("#!/bin/sh\necho codex-cli 0.160.0\n")
    with pytest.raises(ValueError, match="binary version"):
        runtime.check_package(package)


def test_external_runtime_symlink_is_rejected(package: Path):
    host = package / "bin/codex-code-mode-host"
    host.unlink()
    host.symlink_to("/bin/true")
    with pytest.raises(ValueError, match="symlink escapes"):
        runtime.check_package(package, probe=False)


def test_full_file_fingerprint_detects_extra_resource_changes(package: Path):
    before = runtime.fingerprint(package)
    (package / "codex-resources/extra.dat").write_text("new resource")
    assert before != runtime.fingerprint(package)


def test_files_only_check_never_launches_cli_or_host(package: Path, monkeypatch):
    monkeypatch.setattr(runtime.subprocess, "check_output", lambda *args, **kwargs: pytest.fail("CLI was launched"))
    result = runtime.check_package(package, probe=False, hashes=False)
    assert not result["offline_tool_roundtrip"]


def test_startup_gate_reads_actual_configured_runtime(package: Path, tmp_path: Path):
    config = tmp_path / "bridge.toml"
    config.write_text('[bridge]\ncodex_bin = ' + json.dumps(str(package / "bin/codex")) + '\n')
    result = subprocess.run([sys.executable, str(SCRIPTS / "codex_runtime_check.py"), "--config", str(config), "--version", "0.164.0"], capture_output=True, text=True)
    assert result.returncode == 0
    assert json.loads(result.stdout)["offline_tool_roundtrip"]
    (package / "bin/codex-code-mode-host").unlink()
    assert subprocess.run(result.args, capture_output=True).returncode != 0


def test_startup_gate_rejects_unpinned_cli_path(tmp_path: Path):
    config = tmp_path / "bridge.toml"
    config.write_text('[bridge]\ncodex_bin = "codex"\n')
    result = subprocess.run([sys.executable, str(SCRIPTS / "codex_runtime_check.py"), "--config", str(config)], capture_output=True, text=True)
    assert result.returncode != 0
    assert "absolute runtime path" in result.stderr


def test_failed_startup_condition_returns_one_without_restart_failure(package: Path):
    (package / "bin/codex-code-mode-host").unlink()
    result = subprocess.run([sys.executable, str(SCRIPTS / "codex_runtime_check.py"), "--package", str(package), "--condition"], capture_output=True, text=True)
    assert result.returncode == 1
    assert json.loads(result.stdout)["status"] == "FAIL"
    assert not result.stderr


def bootstrap_fixture(package: Path, tmp_path: Path) -> tuple[str, dict, dict, Path]:
    script = ast.parse((SCRIPTS / "fleet-controller.py").read_text())
    assignment = next(node for node in script.body if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "BOOTSTRAP" for target in node.targets))
    bootstrap = ast.literal_eval(assignment.value)
    home = tmp_path / "home"
    request = {"sha": "a" * 40, "codex_version": "0.164.0"}
    stage = home / ".local/share/codex-feishu-bridge/fleet-artifacts" / request["sha"]
    stage.mkdir(parents=True)
    shutil.copytree(package, stage / "codex")
    shutil.copy2(SCRIPTS / "codex_runtime_check.py", stage / "codex_runtime_check.py")
    link = home / ".local/bin/codex"
    link.parent.mkdir(parents=True)
    link.symlink_to("/previous/working/codex")
    return bootstrap, {"home": str(home), "python": sys.executable}, request, stage


def checksum_stage(stage: Path):
    checksums = {path.relative_to(stage).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in stage.rglob("*") if path.is_file() and path.name != "checksums.json"}
    (stage / "checksums.json").write_text(json.dumps(checksums))


def test_controller_rejects_checksum_correct_incomplete_package_before_cli_swap(package: Path, tmp_path: Path, monkeypatch):
    bootstrap, node, request, stage = bootstrap_fixture(package, tmp_path)
    (stage / "codex/bin/codex-code-mode-host").unlink()
    checksum_stage(stage)
    monkeypatch.setattr(sys, "argv", ["bootstrap", json.dumps(node), json.dumps(request)])
    with pytest.raises(ValueError, match="runtime component"):
        exec(bootstrap, {"__name__": "bootstrap_test"})
    assert (Path(node["home"]) / ".local/bin/codex").readlink() == Path("/previous/working/codex")


def test_controller_refuses_bad_existing_install_cache(package: Path, tmp_path: Path, monkeypatch):
    bootstrap, node, request, stage = bootstrap_fixture(package, tmp_path)
    checksum_stage(stage)
    installed = Path(node["home"]) / ".codex/packages/standalone/releases" / (request["codex_version"] + "-yinshi-" + request["sha"][:12])
    installed.mkdir(parents=True)
    (installed / "codex-package.json").write_text((package / "codex-package.json").read_text())
    monkeypatch.setattr(sys, "argv", ["bootstrap", json.dumps(node), json.dumps(request)])
    with pytest.raises(ValueError, match="runtime component"):
        exec(bootstrap, {"__name__": "bootstrap_test"})
    assert (Path(node["home"]) / ".local/bin/codex").readlink() == Path("/previous/working/codex")


def test_controller_refuses_old_artifact_without_validator(package: Path, tmp_path: Path, monkeypatch):
    bootstrap, node, request, stage = bootstrap_fixture(package, tmp_path)
    (stage / "codex_runtime_check.py").unlink()
    checksum_stage(stage)
    monkeypatch.setattr(sys, "argv", ["bootstrap", json.dumps(node), json.dumps(request)])
    with pytest.raises(RuntimeError, match="mandatory runtime validator"):
        exec(bootstrap, {"__name__": "bootstrap_test"})


def test_actual_repaired_runtime_executes_without_model_api():
    actual = Path.home() / ".codex/packages/standalone/releases/0.164.0-yinshi-218ba1a6cfc6"
    if not (actual / "bin/codex-code-mode-host").is_file():
        pytest.skip("repaired local runtime is unavailable")
    assert runtime.check_package(actual, "0.164.0", hashes=False)["offline_tool_roundtrip"]
