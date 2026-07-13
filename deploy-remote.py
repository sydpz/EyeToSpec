#!/usr/bin/env python3
"""Deploy EyeToSpec to the aliyun machine so the owner can view/edit on phone.

EyeToSpec is self-contained Python (serve.py) + static web/. The only external
dependency is the game art: each live pack.json points `repo` at the local
absolute-td checkout and resolves `tex` keys through <repo>/<resourceRoot> and
<repo>/<assetProfiles>. The remote has no such checkout, so we ship the assets
subtree alongside the tool and rewrite each pack's `repo` to the remote path.

Layout on the remote (all under one slot dir, default /root/github/eyetospec):
    serve.py, web/, docs/
    config/absolute-td-live/<pack>/pack.json     (repo rewritten to <dir>/_repo)
    _repo/apps/web-client/public/assets/...       (real game art)
    _repo/apps/web-client/asset-profiles.json

Then: python3 serve.py --host 0.0.0.0 --port 5205 --no-open --config config

SSH credentials reuse the game deploy tooling (ABSOLUTETD_SSH_HOST/_USER/
_PRIVATE_KEY from env or ~/.config/absolute-td/.env).

Port 5205: the owner reserved 5200..5210 for editor/tool use (5200 is the game
backend, so we pick 5205). A port outside the opened range is firewalled off.

Usage:
    python3 deploy-remote.py                 # port 5205
    python3 deploy-remote.py --port 5206 --dry-run
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shlex
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

EYETOSPEC = Path(__file__).resolve().parent
GAME_REPO = Path("/Users/bingwang/github/absolute-td")

# reuse the game repo's SSH helpers + dotenv loader
sys.path.insert(0, str(GAME_REPO))
from scripts.deploy_common import build_ssh_command, write_private_key  # noqa: E402
from scripts.run_slot_deploy import _default_dotenv_candidates, parse_dotenv  # noqa: E402

DEFAULT_PORT = 5205
DEFAULT_DIR = "/root/github/eyetospec"
LIVE_PACKS = ["home", "shop", "challenge", "henhouse", "endless"]
SSH_KEYS = ["ABSOLUTETD_SSH_HOST", "ABSOLUTETD_SSH_USER", "ABSOLUTETD_SSH_PRIVATE_KEY"]
# where the game repo subtree lands on the remote, relative to the slot dir
REMOTE_REPO_SUBDIR = "_repo"


def load_ssh_env() -> dict[str, str]:
    dotenv: dict[str, str] = {}
    for candidate in _default_dotenv_candidates():
        dotenv.update(parse_dotenv(candidate))
    resolved, missing = {}, []
    for key in SSH_KEYS:
        value = os.environ.get(key) or dotenv.get(key)
        (resolved.__setitem__(key, value) if value else missing.append(key))
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
    return resolved


def _add_dir(tar: tarfile.TarFile, src: Path, arc_prefix: str) -> int:
    """Add every file under src to the tar at arc_prefix/<relpath>. Returns count."""
    n = 0
    for path in sorted(src.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            tar.add(path, arcname=f"{arc_prefix}/{path.relative_to(src)}")
            n += 1
    return n


def build_tar_bytes(remote_dir: str) -> tuple[bytes, dict[str, int]]:
    remote_repo = f"{remote_dir}/{REMOTE_REPO_SUBDIR}"
    stats: dict[str, int] = {}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        # tool code
        tar.add(EYETOSPEC / "serve.py", arcname="serve.py")
        stats["web"] = _add_dir(tar, EYETOSPEC / "web", "web")
        stats["docs"] = _add_dir(tar, EYETOSPEC / "docs", "docs")

        # live packs, with repo rewritten to the remote subtree
        pack_n = 0
        for pack in LIVE_PACKS:
            pj = EYETOSPEC / "config" / "absolute-td-live" / pack / "pack.json"
            raw = json.loads(pj.read_text(encoding="utf-8"))
            raw["repo"] = remote_repo
            data = json.dumps(raw, ensure_ascii=False, indent=2).encode("utf-8")
            info = tarfile.TarInfo(name=f"config/absolute-td-live/{pack}/pack.json")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
            pack_n += 1
        stats["packs"] = pack_n

        # game art subtree the packs resolve tex keys against
        stats["assets"] = _add_dir(
            tar, GAME_REPO / "apps/web-client/public/assets",
            f"{REMOTE_REPO_SUBDIR}/apps/web-client/public/assets")
        tar.add(GAME_REPO / "apps/web-client/asset-profiles.json",
                arcname=f"{REMOTE_REPO_SUBDIR}/apps/web-client/asset-profiles.json")
    return buffer.getvalue(), stats


def build_remote_command(remote_dir: str, port: int) -> str:
    quoted_dir = shlex.quote(remote_dir)
    log_path = shlex.quote(f"/tmp/eyetospec-{port}.log")
    serve = shlex.quote(f"{remote_dir}/serve.py")
    config = shlex.quote(f"{remote_dir}/config")
    return (
        f"set -e && "
        f"mkdir -p {quoted_dir} && "
        f"tar -C {quoted_dir} -xzf - && "
        # default python3 on the slot is 3.6 (can't parse PEP 604 / __future__); pick >=3.7
        f"PY=$(for c in python3.12 python3.11 python3.10 python3.9 python3.8 python3.7; do "
        f"command -v $c >/dev/null 2>&1 && echo $c && break; done) && "
        f"[ -n \"$PY\" ] || (echo NO_PY37; exit 1) && "
        f"fuser -k {port}/tcp >/dev/null 2>&1 || true && sleep 1 && "
        f"setsid -f $PY {serve} --host 0.0.0.0 --port {port} --no-open --config {config} "
        f">{log_path} 2>&1 </dev/null && sleep 2 && "
        f"((ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null) | grep -q ':{port} ' "
        f"&& echo EYETOSPEC_UP || (echo EYETOSPEC_DOWN; tail -30 {log_path}; exit 1))"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deploy EyeToSpec to the aliyun slot.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--dir", default=DEFAULT_DIR, help="remote slot directory")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    env = load_ssh_env()
    host = env["ABSOLUTETD_SSH_HOST"]
    tar_bytes, stats = build_tar_bytes(args.dir)

    print(f"host={host}  dir={args.dir}  port={args.port}")
    print(f"ship: web={stats['web']} docs={stats['docs']} packs={stats['packs']} "
          f"assets={stats['assets']}  tar={len(tar_bytes)//1024}KB")
    if args.dry_run:
        return 0

    remote_command = build_remote_command(args.dir, args.port)
    with tempfile.TemporaryDirectory() as tmp:
        key_path = write_private_key(env["ABSOLUTETD_SSH_PRIVATE_KEY"], Path(tmp))
        result = subprocess.run(
            build_ssh_command(env["ABSOLUTETD_SSH_USER"], host, key_path, remote_command),
            input=tar_bytes, capture_output=True)
    stdout = result.stdout.decode("utf-8", "replace")
    stderr = result.stderr.decode("utf-8", "replace")
    if result.returncode != 0 or "EYETOSPEC_UP" not in stdout:
        raise RuntimeError(f"remote deploy failed (rc={result.returncode}):\n{stdout}\n{stderr}")

    print(f"\nEyeToSpec deployed on {host}:{args.port}")
    print("phone URLs:")
    for pack in LIVE_PACKS:
        print(f"  http://{host}:{args.port}/editor.html?pack=absolute-td-live%2F{pack}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
