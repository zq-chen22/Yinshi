#!/usr/bin/env python3
"""Converge SSH-accessible nodes on a promoted, immutable Yinshi release.

No App credentials, SQLite files, chat histories, or attachment directories
are transferred. Artifacts are source Git bundles and verified Python wheels.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shlex
import subprocess


def command(node, argv, *, timeout=120, capture=True):
    if node["alias"] == "local":
        args = argv
    else:
        args = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", node["alias"], shlex.join(argv)]
    return subprocess.run(args, check=True, text=True, capture_output=capture, timeout=timeout)


BOOTSTRAP = r'''
import hashlib,json,pathlib,subprocess,sys
node=json.loads(sys.argv[1]); request=json.loads(sys.argv[2])
home=pathlib.Path(node['home']);state=home/'.local/share/codex-feishu-bridge'
stage=state/'fleet-artifacts'/request['sha'];bare=state/'yinshi.git';release=state/'releases'/request['sha']
for path,digest in json.loads((stage/'checksums.json').read_text()).items():
    file=stage/path
    if hashlib.sha256(file.read_bytes()).hexdigest()!=digest:raise RuntimeError('artifact checksum mismatch: '+path)
cli=home/'.codex/packages/standalone/releases'/(request['codex_version']+'-yinshi-'+request['sha'][:12])
if not cli.exists():
    import shutil
    cli.parent.mkdir(parents=True,exist_ok=True);shutil.copytree(stage/'codex',cli)
bin=home/'.local/bin/codex';bin.parent.mkdir(parents=True,exist_ok=True)
temporary=bin.with_name('codex.yinshi-next');temporary.unlink(missing_ok=True);temporary.symlink_to(cli/'bin/codex');temporary.replace(bin)
if not bare.exists():subprocess.run(['git','clone','--bare',str(stage/'source.bundle'),str(bare)],check=True,stdout=subprocess.DEVNULL)
else:subprocess.run(['git','-C',str(bare),'fetch',str(stage/'source.bundle'),'+refs/heads/*:refs/heads/*','refs/tags/*:refs/tags/*'],check=True,stdout=subprocess.DEVNULL)
subprocess.run(['git','-C',str(bare),'remote','set-url','origin','https://github.com/zq-chen22/Yinshi.git'],check=True)
if not release.exists():subprocess.run(['git','-C',str(bare),'worktree','add','--detach',str(release),request['sha']],check=True,stdout=subprocess.DEVNULL)
root=release/'codex-feishu-bridge';venv=root/'.venv'
if not (venv/'bin/python').exists():subprocess.run([node['python'],'-m','venv',str(venv)],check=True)
wheel=next((stage/'wheelhouse').glob('codex_feishu_bridge-'+request['bridge_version']+'-*.whl'))
subprocess.run([str(venv/'bin/python'),'-m','pip','install','--no-index','--find-links',str(stage/'wheelhouse'),str(wheel)],check=True,stdout=subprocess.DEVNULL)
request['release_dir']=str(release);request['codex_bin']=str(home/'.local/bin/codex')
req=state/'deployment-request.json';tmp=req.with_suffix('.tmp');tmp.write_text(json.dumps(request,indent=2)+'\n');tmp.chmod(0o600);tmp.replace(req)
unit=home/'.config/systemd/user';unit.mkdir(parents=True,exist_ok=True)
(unit/'codex-feishu-release-activate.service').write_text('[Unit]\nDescription=Activate promoted Yinshi release only when idle\n[Service]\nType=oneshot\nEnvironment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin\nEnvironmentFile=-%h/.config/codex-feishu-bridge/secrets.env\nExecStart="'+str(venv/'bin/python')+'" -m codex_feishu_bridge.fleet activate\nTimeoutStartSec=21760\nUMask=0077\n')
(unit/'codex-feishu-release-activate.timer').write_text('[Unit]\nDescription=Wait safely for an idle Yinshi deployment window\n[Timer]\nOnActiveSec=15s\nOnUnitInactiveSec=30s\nUnit=codex-feishu-release-activate.service\n[Install]\nWantedBy=timers.target\n')
subprocess.run(['systemctl','--user','daemon-reload'],check=True)
subprocess.run(['systemctl','--user','enable','--now','codex-feishu-release-activate.timer'],check=True,stdout=subprocess.DEVNULL)
print(json.dumps({'staged':request['sha'],'release_dir':str(release)}))
'''


def stage_node(node, request, artifact):
    home = node["home"]
    destination = f"{home}/.local/share/codex-feishu-bridge/fleet-artifacts/{request['sha']}"
    command(node, ["mkdir", "-p", destination])
    tar = subprocess.Popen(["tar", "-czf", "-", "-C", str(artifact), "."], stdout=subprocess.PIPE)
    if node["alias"] == "local":
        receiver = ["tar", "-xzf", "-", "-C", destination]
    else:
        receiver = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", node["alias"], shlex.join(["tar", "-xzf", "-", "-C", destination])]
    try:
        subprocess.run(receiver, stdin=tar.stdout, check=True, timeout=300)
    finally:
        if tar.stdout:
            tar.stdout.close()
        tar.wait(timeout=30)
    if tar.returncode:
        raise RuntimeError("artifact stream failed")
    result = command(node, [node["python"], "-c", BOOTSTRAP, json.dumps(node), json.dumps(request)], timeout=600)
    return {"node": node["alias"], "result": json.loads(result.stdout.splitlines()[-1])}


def node_status(node):
    code = "import json,pathlib,subprocess; h=pathlib.Path.home(); p=h/'.local/share/codex-feishu-bridge/deployment.json'; r=json.loads(p.read_text()) if p.exists() else {}; r['service_active']=subprocess.run(['systemctl','--user','is-active','--quiet','codex-feishu-bridge.service']).returncode==0; pending=h/'.local/share/codex-feishu-bridge/deployment-request.json'; r['pending_sha']=json.loads(pending.read_text()).get('sha') if pending.exists() else None; r['activation_timer_active']=subprocess.run(['systemctl','--user','is-active','--quiet','codex-feishu-release-activate.timer']).returncode==0; r['actual_codex']=subprocess.check_output([str(h/'.local/bin/codex'),'--version'],text=True).strip().split()[-1]; print(json.dumps(r))"
    try:
        return {"node": node["alias"], "reachable": True, **json.loads(command(node, [node["python"], "-c", code], timeout=25).stdout)}
    except (subprocess.SubprocessError, ValueError, OSError) as error:
        return {"node": node["alias"], "reachable": False, "error_class": type(error).__name__}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["reconcile", "status"])
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    inventory = json.loads(args.inventory.read_text())
    if args.refresh:
        subprocess.run(["git", "-C", str(args.repo), "fetch", "origin", "main", "--tags"], check=True, timeout=60)
        channel = json.loads(subprocess.check_output(["git", "-C", str(args.repo), "show", "origin/main:deploy/stable.json"], text=True))
    else:
        channel = json.loads((args.repo / "deploy/stable.json").read_text())
    tag = channel["tag"]
    sha = subprocess.check_output(["git", "-C", str(args.repo), "rev-parse", tag + "^{commit}"], text=True).strip()
    request = {**channel, "sha": sha, "policy": inventory["policy"], "policy_revision": inventory["policy_revision"]}
    results = []
    for node in inventory["nodes"]:
        status = node_status(node)
        results.append(status)
        converged = status.get("sha") == sha and status.get("service_active") and status.get("actual_codex") == channel["codex_version"] and status.get("policy_revision") == inventory["policy_revision"]
        waiting = status.get("pending_sha") == sha and status.get("activation_timer_active")
        if args.mode == "reconcile" and status.get("reachable") and not converged and not waiting:
            artifact = args.artifact or Path(inventory["artifacts_root"]) / sha
            if not artifact.is_dir():
                raise RuntimeError("promoted artifacts are missing; build/verify the release before deployment")
            try:
                results.append(stage_node(node, request, artifact))
            except (subprocess.SubprocessError, OSError, ValueError, RuntimeError) as error:
                results.append({"node": node["alias"], "stage_failed": True, "error_class": type(error).__name__})
    print(json.dumps({"desired_sha": sha, "desired_tag": tag, "nodes": results}, ensure_ascii=False, indent=2))
    return 1 if any(r.get("stage_failed") for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
