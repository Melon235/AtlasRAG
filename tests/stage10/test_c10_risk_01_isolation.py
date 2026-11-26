"""Non-destructive adversarial isolation regressions for C10-RISK-01."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from integration import stage10_helpers

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
STAGE10_VERIFY_SCRIPT = REPOSITORY_ROOT / "scripts" / "stage10_verify.sh"
MAKEFILE_PATH = REPOSITORY_ROOT / "Makefile"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _create_fake_docker_and_uv(
    tmp_path: Path,
    *,
    existing_containers: dict[str, dict[str, Any]] | None = None,
    existing_volumes: dict[str, dict[str, Any]] | None = None,
    existing_networks: dict[str, dict[str, Any]] | None = None,
    fail_up: bool = False,
    partial_up_services: tuple[str, ...] = (),
    port_override_by_service: dict[str, int] | None = None,
    port_host_override: str | None = None,
) -> tuple[Path, Path, Path, Path]:
    """Create non-destructive fake ``docker`` and ``uv`` binaries on disk."""

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log_path = tmp_path / "docker_calls.jsonl"
    uv_log_path = tmp_path / "uv_calls.jsonl"
    state_path = tmp_path / "docker_state.json"
    state_path.write_text(
        json.dumps(
            {
                "containers": existing_containers or {},
                "volumes": existing_volumes or {},
                "networks": existing_networks or {},
                "fail_up": fail_up,
                "partial_up_services": list(partial_up_services),
                "port_override_by_service": port_override_by_service or {},
                "port_host_override": port_host_override,
            }
        ),
        encoding="utf-8",
    )

    docker_script = f"""#!{sys.executable}
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

LOG_PATH = Path({str(log_path)!r})
STATE_PATH = Path({str(state_path)!r})
args = sys.argv[1:]

with LOG_PATH.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps({{"argv": args, "project": os.environ.get("COMPOSE_PROJECT_NAME", "")}}) + "\\n")

state = json.loads(STATE_PATH.read_text(encoding="utf-8"))

def save_state() -> None:
    STATE_PATH.write_text(json.dumps(state), encoding="utf-8")

def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {{}}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        values[k.strip()] = v.strip().strip('"').strip("'")
    # Resolve simple ${{VAR}} expressions against values then os.environ
    for _ in range(3):
        for k, v in list(values.items()):
            values[k] = re.sub(
                r"\\$\\{{([A-Za-z_][A-Za-z0-9_]*)\\}}",
                lambda m: os.environ.get(m.group(1), values.get(m.group(1), "")),
                v,
            )
    return values

def extract_labels_from_override(paths: list[Path]) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    svc_labels: dict[str, dict[str, str]] = {{}}
    vol_labels: dict[str, dict[str, str]] = {{}}
    net_labels: dict[str, dict[str, str]] = {{}}
    for p in paths:
        if not p.is_file():
            continue
        txt = p.read_text(encoding="utf-8")
        run_m = re.search(r'atlasrag\\.stage10b\\.run_id:\\s*["\\x27]?([^"\\x27\\s]+)', txt)
        proj_m = re.search(r'atlasrag\\.stage10b\\.project:\\s*["\\x27]?([^"\\x27\\s]+)', txt)
        if run_m:
            base = {{"atlasrag.stage10b.run_id": run_m.group(1)}}
            if proj_m:
                base["atlasrag.stage10b.project"] = proj_m.group(1)
            for s in ("postgres", "redis", "etcd", "minio", "milvus"):
                svc_labels[s] = dict(base)
            for v in ("postgres_data", "etcd_data", "minio_data", "milvus_data"):
                vol_labels[v] = dict(base)
            net_labels["atlasrag_internal"] = dict(base)
    return svc_labels, vol_labels, net_labels

def filter_matches(labels: dict[str, str], filters: list[str]) -> bool:
    for f in filters:
        if f.startswith("label="):
            expr = f[len("label="):]
            if "=" in expr:
                k, expected = expr.split("=", 1)
                if labels.get(k) != expected:
                    return False
            elif expr not in labels:
                return False
    return True

if not args:
    sys.exit(0)

if args[0] == "info":
    sys.exit(0)

if args[0] == "compose":
    i = 1
    env_file = None
    compose_files: list[Path] = []
    project = os.environ.get("COMPOSE_PROJECT_NAME", "")
    while i < len(args):
        tok = args[i]
        if tok == "--env-file" and i + 1 < len(args):
            env_file = Path(args[i + 1])
            i += 2
        elif tok in ("-f", "--file") and i + 1 < len(args):
            compose_files.append(Path(args[i + 1]))
            i += 2
        elif tok in ("-p", "--project-name") and i + 1 < len(args):
            project = args[i + 1]
            i += 2
        else:
            break
    rest = args[i:]
    if not rest:
        sys.exit(0)
    sub = rest[0]
    env_vals = parse_env_file(env_file) if env_file else {{}}
    if sub == "ps":
        quiet = "-q" in rest
        service = rest[-1] if rest[-1] not in ("ps", "-a", "-q") else None
        for cid, info in state["containers"].items():
            if info["labels"].get("com.docker.compose.project") != project:
                continue
            if service and info["labels"].get("com.docker.compose.service") != service:
                continue
            print(cid if quiet else json.dumps(info))
        sys.exit(0)
    if sub == "up":
        svc_labels, vol_labels, net_labels = extract_labels_from_override(compose_files[1:])
        partial = state["partial_up_services"]
        services = partial if state["fail_up"] and partial else (
            [] if state["fail_up"] else ["postgres", "redis", "etcd", "minio", "milvus"]
        )
        pg_port = int(state["port_override_by_service"].get("postgres", env_vals.get("POSTGRES_PORT", 5432)))
        rd_port = int(state["port_override_by_service"].get("redis", env_vals.get("REDIS_PORT", 6379)))
        mv_port = int(state["port_override_by_service"].get("milvus", env_vals.get("MILVUS_PORT", 19530)))
        bind_ip = state["port_host_override"] or env_vals.get("ATLASRAG_BIND_HOST", "127.0.0.1")
        port_map = {{
            "postgres": {{"5432/tcp": [{{"HostIp": bind_ip, "HostPort": str(pg_port)}}]}},
            "redis": {{"6379/tcp": [{{"HostIp": bind_ip, "HostPort": str(rd_port)}}]}},
            "etcd": {{}},
            "minio": {{}},
            "milvus": {{"19530/tcp": [{{"HostIp": bind_ip, "HostPort": str(mv_port)}}]}},
        }}
        env_map = {{
            "postgres": [
                f"POSTGRES_DB={{env_vals.get('POSTGRES_DB', 'atlasrag')}}",
                f"POSTGRES_USER={{env_vals.get('POSTGRES_USER', 'atlasrag')}}",
            ],
            "redis": [],
            "etcd": [],
            "minio": [],
            "milvus": ["ETCD_ENDPOINTS=etcd:2379", "MINIO_ADDRESS=minio:9000"],
        }}
        if services:
            for net_name in ("atlasrag_internal",):
                full_net = f"{{project}}_{{net_name}}"
                n_lbls = dict(net_labels.get(net_name, {{}}))
                n_lbls["com.docker.compose.project"] = project
                state["networks"][full_net] = {{"name": full_net, "labels": n_lbls}}
            for vol_name in ("postgres_data", "etcd_data", "minio_data", "milvus_data"):
                full_vol = f"{{project}}_{{vol_name}}"
                v_lbls = dict(vol_labels.get(vol_name, {{}}))
                v_lbls["com.docker.compose.project"] = project
                state["volumes"][full_vol] = {{"name": full_vol, "labels": v_lbls}}
        for svc in services:
            cid = f"{{project}}-{{svc}}-1"
            c_lbls = dict(svc_labels.get(svc, {{}}))
            c_lbls["com.docker.compose.project"] = project
            c_lbls["com.docker.compose.service"] = svc
            state["containers"][cid] = {{
                "Id": cid,
                "State": {{"Running": True, "Health": {{"Status": "healthy"}}}},
                "Config": {{"Labels": c_lbls, "Env": env_map[svc]}},
                "NetworkSettings": {{"Ports": port_map[svc]}},
                "labels": c_lbls,
            }}
        save_state()
        sys.exit(1 if state["fail_up"] else 0)
    if sub == "port":
        svc, internal = rest[1], rest[2]
        key = f"{{internal}}/tcp"
        for info in state["containers"].values():
            lbls = info.get("labels", {{}})
            if lbls.get("com.docker.compose.project") == project and lbls.get("com.docker.compose.service") == svc:
                bindings = info["NetworkSettings"]["Ports"].get(key, [])
                if bindings:
                    print(f"{{bindings[0]['HostIp']}}:{{bindings[0]['HostPort']}}")
                    sys.exit(0)
        sys.exit(1)
    if sub == "down":
        for cid in list(state["containers"]):
            if state["containers"][cid]["labels"].get("com.docker.compose.project") == project:
                del state["containers"][cid]
        if "-v" in rest:
            for vid in list(state["volumes"]):
                if state["volumes"][vid]["labels"].get("com.docker.compose.project") == project:
                    del state["volumes"][vid]
        for nid in list(state["networks"]):
            if state["networks"][nid]["labels"].get("com.docker.compose.project") == project:
                del state["networks"][nid]
        save_state()
        sys.exit(0)
    if sub == "logs":
        sys.exit(0)
    sys.exit(0)

if args[0] == "ps":
    filters = [args[i + 1] for i in range(len(args) - 1) if args[i] in ("-f", "--filter")]
    for cid, info in state["containers"].items():
        if filter_matches(info.get("labels", {{}}), filters):
            print(cid)
    sys.exit(0)

if args[0] == "volume" and len(args) > 1:
    if args[1] == "ls":
        filters = [args[i + 1] for i in range(len(args) - 1) if args[i] in ("-f", "--filter")]
        for vid, info in state["volumes"].items():
            if filter_matches(info.get("labels", {{}}), filters):
                print(vid)
        sys.exit(0)
    if args[1] == "rm":
        for vid in args[2:]:
            if not vid.startswith("-"):
                state["volumes"].pop(vid, None)
        save_state()
        sys.exit(0)

if args[0] == "network" and len(args) > 1:
    if args[1] == "ls":
        filters = [args[i + 1] for i in range(len(args) - 1) if args[i] in ("-f", "--filter")]
        for nid, info in state["networks"].items():
            if filter_matches(info.get("labels", {{}}), filters):
                print(nid)
        sys.exit(0)
    if args[1] == "rm":
        for nid in args[2:]:
            if not nid.startswith("-"):
                state["networks"].pop(nid, None)
        save_state()
        sys.exit(0)

if args[0] == "rm":
    for cid in args[1:]:
        if not cid.startswith("-"):
            state["containers"].pop(cid, None)
    save_state()
    sys.exit(0)

if args[0] == "logs":
    sys.exit(0)

if args[0] == "inspect":
    targets = [tok for tok in args[1:] if not tok.startswith("-")]
    fmt = ""
    for i, tok in enumerate(args):
        if tok in ("-f", "--format") and i + 1 < len(args):
            fmt = args[i + 1]
    out = []
    for t in targets:
        info = state["containers"].get(t)
        if info is None:
            sys.exit(1)
        out.append(
            {{
                "Id": info["Id"],
                "State": info["State"],
                "Config": info["Config"],
                "NetworkSettings": info["NetworkSettings"],
            }}
        )
    if fmt:
        print(json.dumps(out[0]))
    else:
        print(json.dumps(out))
    sys.exit(0)

sys.exit(0)
"""
    _write_executable(bin_dir / "docker", docker_script)

    uv_script = f"""#!{sys.executable}
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

UV_LOG = Path({str(uv_log_path)!r})
args = sys.argv[1:]
loaded_env: dict[str, str] = {{}}
for idx, tok in enumerate(args):
    if tok == "--env-file" and idx + 1 < len(args):
        env_p = Path(args[idx + 1])
        if env_p.is_file():
            for raw in env_p.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    loaded_env[k.strip()] = v.strip()
effective = {{**loaded_env, **os.environ}}
with UV_LOG.open("a", encoding="utf-8") as fh:
    fh.write(
        json.dumps(
            {{
                "argv": args,
                "ATLASRAG_ENV_FILE": effective.get("ATLASRAG_ENV_FILE"),
                "ATLASRAG_STAGE10B_ENV_FILE": effective.get("ATLASRAG_STAGE10B_ENV_FILE"),
                "ATLASRAG_POSTGRES_DSN": effective.get("ATLASRAG_POSTGRES_DSN"),
                "ATLASRAG_REDIS_URL": effective.get("ATLASRAG_REDIS_URL"),
                "ATLASRAG_TEST_REDIS_URL": effective.get("ATLASRAG_TEST_REDIS_URL"),
                "ATLASRAG_MILVUS_URI": effective.get("ATLASRAG_MILVUS_URI"),
                "ATLASRAG_STAGE10B_ISOLATED": effective.get("ATLASRAG_STAGE10B_ISOLATED"),
                "ATLASRAG_STAGE10B_RUN_ID": effective.get("ATLASRAG_STAGE10B_RUN_ID"),
                "ATLASRAG_STAGE10B_OWNER_TOKEN": effective.get("ATLASRAG_STAGE10B_OWNER_TOKEN"),
                "ATLASRAG_STAGE10B_COMPOSE_PROJECT": effective.get("ATLASRAG_STAGE10B_COMPOSE_PROJECT"),
                "ATLASRAG_STAGE10B_PG_CONTAINER": effective.get("ATLASRAG_STAGE10B_PG_CONTAINER"),
                "ATLASRAG_STAGE10B_REDIS_CONTAINER": effective.get("ATLASRAG_STAGE10B_REDIS_CONTAINER"),
                "ATLASRAG_STAGE10B_MILVUS_CONTAINER": effective.get("ATLASRAG_STAGE10B_MILVUS_CONTAINER"),
            }}
        ) + "\\n"
    )
sys.exit(0)
"""
    _write_executable(bin_dir / "uv", uv_script)
    return bin_dir, log_path, uv_log_path, state_path


def _run_verify_script(
    bin_dir: Path,
    *,
    env_overrides: Mapping[str, str | None] | None = None,
    lock_dir: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    for var in (
        "ATLASRAG_ENV_FILE",
        "COMPOSE_PROJECT_NAME",
        "COMPOSE_FILE",
        "ATLASRAG_POSTGRES_DSN",
        "ATLASRAG_REDIS_URL",
        "ATLASRAG_TEST_REDIS_URL",
        "ATLASRAG_MILVUS_URI",
        "ATLASRAG_MILVUS_COLLECTION",
        "POSTGRES_PORT",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "REDIS_PORT",
        "MILVUS_PORT",
        "MILVUS_HEALTH_PORT",
        "SEARXNG_PORT",
        "SEARXNG_SECRET",
        "MINIO_ROOT_USER",
        "MINIO_ROOT_PASSWORD",
        "ATLASRAG_BIND_HOST",
        "ATLASRAG_STAGE10B_ISOLATED",
        "ATLASRAG_STAGE10B_RUN_ID",
        "ATLASRAG_STAGE10B_OWNER_TOKEN",
        "ATLASRAG_STAGE10B_COMPOSE_PROJECT",
        "ATLASRAG_STAGE10B_PG_CONTAINER",
        "ATLASRAG_STAGE10B_REDIS_CONTAINER",
        "ATLASRAG_STAGE10B_MILVUS_CONTAINER",
        "ATLASRAG_STAGE10B_ENV_FILE",
    ):
        env.pop(var, None)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    if lock_dir is not None:
        env["STAGE10_LOCK_DIR"] = str(lock_dir)
    if env_overrides:
        for k, v in env_overrides.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
    return subprocess.run(
        ["bash", str(STAGE10_VERIFY_SCRIPT), "--integration-only"],
        cwd=REPOSITORY_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _has_destructive_docker_call(calls: list[dict[str, Any]]) -> bool:
    for entry in calls:
        argv = entry.get("argv", [])
        if argv[:2] == ["compose", "down"] or "down" in argv:
            return True
        if (
            argv[:1] == ["rm"]
            or argv[:2] == ["volume", "rm"]
            or argv[:2] == ["network", "rm"]
        ):
            return True
    return False


@dataclass
class _FakeStage10Redis:
    """In-memory Redis double supporting SCAN, UNLINK, GET, SET NX, DBSIZE, EVAL."""

    db0: dict[bytes, bytes] = field(default_factory=dict)
    unlink_calls: list[tuple[bytes, ...]] = field(default_factory=list)
    set_calls: list[tuple[bytes, bytes]] = field(default_factory=list)
    eval_calls: int = 0

    async def ping(self) -> bool:
        return True

    async def get(self, key: str | bytes) -> bytes | None:
        raw = key.encode("utf-8") if isinstance(key, str) else key
        return self.db0.get(raw)

    async def set(
        self, key: str | bytes, value: str | bytes, *, nx: bool = False
    ) -> bool | None:
        raw_k = key.encode("utf-8") if isinstance(key, str) else key
        raw_v = value.encode("utf-8") if isinstance(value, str) else value
        self.set_calls.append((raw_k, raw_v))
        if nx and raw_k in self.db0:
            return None
        self.db0[raw_k] = raw_v
        return True

    async def dbsize(self) -> int:
        return len(self.db0)

    async def scan(
        self, *, cursor: int = 0, count: int = 200
    ) -> tuple[int, list[bytes]]:
        _ = count
        return (0, list(self.db0.keys())) if cursor == 0 else (0, [])

    async def unlink(self, *keys: str | bytes) -> int:
        raw = tuple(k.encode("utf-8") if isinstance(k, str) else k for k in keys)
        self.unlink_calls.append(raw)
        removed = 0
        for k in raw:
            if k in self.db0:
                del self.db0[k]
                removed += 1
        return removed

    async def eval(self, script: str, numkeys: int, *args: str | bytes) -> int:
        self.eval_calls += 1
        keys = [a.encode("utf-8") if isinstance(a, str) else a for a in args[:numkeys]]
        vals = [a.encode("utf-8") if isinstance(a, str) else a for a in args[numkeys:]]
        if "GET', KEYS[1]) ~= ARGV[1]" in script:
            if self.db0.get(keys[0]) != vals[0]:
                return -1
            deleted = 0
            for k in keys[1:]:
                if k in self.db0:
                    del self.db0[k]
                    deleted += 1
            return deleted
        raise NotImplementedError(script)

    async def aclose(self) -> None:
        return None


@dataclass
class _FakeStage10Milvus:
    collections: dict[str, dict[str, Any]] = field(default_factory=dict)
    dropped: list[str] = field(default_factory=list)

    def has_collection(self, collection_name: str, **_kwargs: Any) -> bool:
        return collection_name in self.collections

    def list_collections(self) -> list[str]:
        return list(self.collections.keys())

    def describe_collection(
        self, collection_name: str, **_kwargs: Any
    ) -> dict[str, Any]:
        return dict(self.collections[collection_name])

    def drop_collection(self, collection_name: str, **_kwargs: Any) -> None:
        self.dropped.append(collection_name)
        self.collections.pop(collection_name, None)

    def close(self) -> None:
        return None


def test_c10_risk_01_makefile_decouples_from_default_dev_env() -> None:
    makefile = MAKEFILE_PATH.read_text(encoding="utf-8")
    m = re.search(
        r"^test-stage10-integration:\n((?:\t.*\n)+)",
        makefile,
        re.MULTILINE,
    )
    assert m is not None
    assert "ATLASRAG_ENV_FILE=" not in m.group(1)


def test_c10_risk_01_case01_rejects_dev_compose_project_name(tmp_path: Path) -> None:
    bin_dir, log_path, uv_log_path, state_path = _create_fake_docker_and_uv(
        tmp_path,
        existing_volumes={
            "atlasrag-local_postgres_data": {
                "name": "atlasrag-local_postgres_data",
                "labels": {"com.docker.compose.project": "atlasrag-local"},
            }
        },
    )
    res = _run_verify_script(
        bin_dir,
        env_overrides={"COMPOSE_PROJECT_NAME": "atlasrag-local"},
        lock_dir=tmp_path / "locks",
    )
    assert res.returncode != 0
    assert not _has_destructive_docker_call(_read_jsonl(log_path))
    assert not _read_jsonl(uv_log_path)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert "atlasrag-local_postgres_data" in state["volumes"]


def test_c10_risk_01_case02_rejects_stage9_pilot_project_name(tmp_path: Path) -> None:
    bin_dir, log_path, uv_log_path, state_path = _create_fake_docker_and_uv(
        tmp_path,
        existing_volumes={
            "atlasrag-stage9-realpilot_milvus_data": {
                "name": "atlasrag-stage9-realpilot_milvus_data",
                "labels": {"com.docker.compose.project": "atlasrag-stage9-realpilot"},
            }
        },
    )
    res = _run_verify_script(
        bin_dir,
        env_overrides={"COMPOSE_PROJECT_NAME": "atlasrag-stage9-realpilot"},
        lock_dir=tmp_path / "locks",
    )
    assert res.returncode != 0
    assert not _has_destructive_docker_call(_read_jsonl(log_path))
    assert not _read_jsonl(uv_log_path)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert "atlasrag-stage9-realpilot_milvus_data" in state["volumes"]


def test_c10_risk_01_case03_rejects_test_prefix_project_with_existing_containers(
    tmp_path: Path,
) -> None:
    bin_dir, log_path, uv_log_path, state_path = _create_fake_docker_and_uv(
        tmp_path,
        existing_containers={
            "existing-pg": {
                "Id": "existing-pg",
                "State": {"Running": True, "Health": {"Status": "healthy"}},
                "Config": {
                    "Labels": {
                        "com.docker.compose.project": "atlasrag-stage10-existing"
                    },
                    "Env": [],
                },
                "NetworkSettings": {"Ports": {}},
                "labels": {
                    "com.docker.compose.project": "atlasrag-stage10-existing",
                    "com.docker.compose.service": "postgres",
                },
            }
        },
    )
    res = _run_verify_script(
        bin_dir,
        env_overrides={"COMPOSE_PROJECT_NAME": "atlasrag-stage10-existing"},
        lock_dir=tmp_path / "locks",
    )
    assert res.returncode != 0
    assert not _has_destructive_docker_call(_read_jsonl(log_path))
    assert not _read_jsonl(uv_log_path)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert "existing-pg" in state["containers"]


def test_c10_risk_01_case04_rejects_existing_project_volumes_and_networks(
    tmp_path: Path,
) -> None:
    for kind in ("volume", "network"):
        sub = tmp_path / kind
        sub.mkdir()
        vols = (
            {
                "atlasrag-stage10-dirty_postgres_data": {
                    "name": "atlasrag-stage10-dirty_postgres_data",
                    "labels": {"com.docker.compose.project": "atlasrag-stage10-dirty"},
                }
            }
            if kind == "volume"
            else {}
        )
        nets = (
            {
                "atlasrag-stage10-dirty_atlasrag_internal": {
                    "name": "atlasrag-stage10-dirty_atlasrag_internal",
                    "labels": {"com.docker.compose.project": "atlasrag-stage10-dirty"},
                }
            }
            if kind == "network"
            else {}
        )
        bin_dir, log_path, uv_log_path, state_path = _create_fake_docker_and_uv(
            sub,
            existing_volumes=vols,
            existing_networks=nets,
        )
        res = _run_verify_script(
            bin_dir,
            env_overrides={"COMPOSE_PROJECT_NAME": "atlasrag-stage10-dirty"},
            lock_dir=sub / "locks",
        )
        assert res.returncode != 0
        assert not _has_destructive_docker_call(_read_jsonl(log_path))
        assert not _read_jsonl(uv_log_path)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if kind == "volume":
            assert "atlasrag-stage10-dirty_postgres_data" in state["volumes"]
        else:
            assert "atlasrag-stage10-dirty_atlasrag_internal" in state["networks"]


@pytest.mark.asyncio
async def test_c10_risk_01_case05_rejects_non_test_redis_endpoint_and_wrong_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeStage10Redis(db0={b"user:data": b"must-survive"})
    monkeypatch.setattr(stage10_helpers.Redis, "from_url", lambda *_a, **_kw: fake)
    # No Stage 10-B isolation context -> clear_stage10_redis_db0 must refuse BEFORE unlink.
    with pytest.raises(Exception):
        await stage10_helpers.clear_stage10_redis_db0("127.0.0.1", 16379)
    assert fake.unlink_calls == []
    assert fake.eval_calls == 0
    assert fake.db0 == {b"user:data": b"must-survive"}

    # Even with ATLASRAG_REDIS_URL set to non-/0 DB, parse_stage10_endpoints must refuse.
    monkeypatch.setenv(
        "ATLASRAG_POSTGRES_DSN", "postgresql://u:p@127.0.0.1:25432/atlasrag_s10"
    )
    monkeypatch.setenv("ATLASRAG_REDIS_URL", "redis://127.0.0.1:26379/3")
    monkeypatch.setenv("ATLASRAG_MILVUS_URI", "http://127.0.0.1:29530")
    with pytest.raises(Exception):
        stage10_helpers.parse_stage10_endpoints()


def test_c10_risk_01_case06_rejects_non_test_milvus_endpoint_without_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_milvus = _FakeStage10Milvus(
        collections={
            "atlas_chunks": {
                "collection_name": "atlas_chunks",
                "description": "stage9-pilot",
            }
        }
    )
    monkeypatch.setattr(stage10_helpers, "MilvusClient", lambda **_kw: fake_milvus)
    with pytest.raises(Exception):
        stage10_helpers.drop_stage10_milvus_collection("http://127.0.0.1:19530")
    assert fake_milvus.dropped == []
    assert "atlas_chunks" in fake_milvus.collections


@pytest.mark.asyncio
async def test_c10_risk_01_case07_and_case11_direct_pytest_without_ownership_blocks_before_pg_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct pytest and unit helpers refuse non-test PG endpoints and unowned runs before CREATE DATABASE."""
    from scripts.stage10_isolation import (
        Stage10IsolationError,
        resolve_and_validate_effective_env,
        verify_stage10_service_ownership,
    )

    # Rejects deploy/local/.env and non-loopback/mismatched PG endpoints.
    dev_env_file = REPOSITORY_ROOT / "deploy" / "local" / ".env.example"
    custom_env = tmp_path / "custom.env"
    custom_env.write_text(
        dev_env_file.read_text(encoding="utf-8")
        + "\nPOSTGRES_PORT=5432\nREDIS_PORT=6379\nMILVUS_PORT=19530\n"
        "ATLASRAG_POSTGRES_DSN=postgresql://atlasrag:pw@127.0.0.1:5432/atlasrag\n"
        "ATLASRAG_REDIS_URL=redis://127.0.0.1:6379/0\n"
        "ATLASRAG_TEST_REDIS_URL=redis://127.0.0.1:6379/15\n"
        "ATLASRAG_MILVUS_URI=http://127.0.0.1:19530\n",
        encoding="utf-8",
    )
    with pytest.raises(Stage10IsolationError, match="Conflicting shell and env-file"):
        resolve_and_validate_effective_env(
            custom_env,
            {"ATLASRAG_REDIS_URL": "redis://127.0.0.1:26379/0"},
            repository_root=REPOSITORY_ROOT,
        )

    with pytest.raises(Stage10IsolationError, match="ownership proof"):
        verify_stage10_service_ownership({})

    # Direct pytest invocation pointing to closed high loopback ports must fail in ownership guard,
    # never reaching Postgres CREATE DATABASE.
    env = dict(os.environ)
    env["ATLASRAG_RUN_INTEGRATION"] = "1"
    env["ATLASRAG_POSTGRES_DSN"] = (
        "postgresql://atlasrag:secret@127.0.0.1:59991/atlasrag"
    )
    env["ATLASRAG_REDIS_URL"] = "redis://127.0.0.1:59992/0"
    env["ATLASRAG_TEST_REDIS_URL"] = "redis://127.0.0.1:59992/15"
    env["ATLASRAG_MILVUS_URI"] = "http://127.0.0.1:59993"
    for key in list(env):
        if key.startswith("ATLASRAG_STAGE10B_"):
            env.pop(key, None)

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/integration/test_stage10_runtime_lifecycle.py",
            "-k",
            "test_b10_03_empty_kb_startup_ownership_and_startup_retry",
            "-m",
            "integration",
        ],
        cwd=REPOSITORY_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode != 0
    assert "Stage 10-B ownership proof missing" in completed.stdout + completed.stderr

    # Even with fake token set, missing per-container PG identity fails before touching PG.
    monkeypatch.setenv("ATLASRAG_STAGE10B_ISOLATED", "1")
    monkeypatch.setenv("ATLASRAG_STAGE10B_RUN_ID", "atlasrag_s10b_0123456789abcdef")
    monkeypatch.setenv("ATLASRAG_STAGE10B_OWNER_TOKEN", "tok")
    monkeypatch.setenv("ATLASRAG_STAGE10B_COMPOSE_PROJECT", "atlasrag-stage10b-unit")
    monkeypatch.setenv("ATLASRAG_STAGE10B_ENV_FILE", str(custom_env))
    with pytest.raises(Stage10IsolationError, match="container identity"):
        verify_stage10_service_ownership()


def test_c10_risk_01_case08_rejects_conflicting_shell_and_env_file_overrides(
    tmp_path: Path,
) -> None:
    dev_env = tmp_path / "deploy" / "local" / ".env"
    dev_env.parent.mkdir(parents=True)
    dev_env.write_text(
        "ATLASRAG_REDIS_URL=redis://127.0.0.1:6379/0\n", encoding="utf-8"
    )

    for bad_env in (
        {
            "ATLASRAG_ENV_FILE": str(
                REPOSITORY_ROOT / "deploy" / "local" / ".env.example"
            )
        },
        {"ATLASRAG_REDIS_URL": "redis://127.0.0.1:6379/0"},
        {"ATLASRAG_POSTGRES_DSN": "postgresql://u:p@127.0.0.1:5432/atlasrag"},
        {"ATLASRAG_MILVUS_URI": "http://127.0.0.1:19530"},
        {"REDIS_PORT": "6379"},
    ):
        sub = tmp_path / ("conflict_" + "_".join(bad_env.keys()))
        sub.mkdir()
        bin_dir, log_path, uv_log_path, _ = _create_fake_docker_and_uv(sub)
        res = _run_verify_script(bin_dir, env_overrides=bad_env, lock_dir=sub / "locks")
        assert res.returncode != 0
        assert not _has_destructive_docker_call(_read_jsonl(log_path))
        assert not _read_jsonl(uv_log_path)


def test_c10_risk_01_case09_rejects_published_port_mismatch_before_pytest(
    tmp_path: Path,
) -> None:
    bin_dir, log_path, uv_log_path, state_path = _create_fake_docker_and_uv(
        tmp_path,
        port_override_by_service={"redis": 6379},
    )
    res = _run_verify_script(bin_dir, lock_dir=tmp_path / "locks")
    assert res.returncode != 0
    # Wait/pytest must NEVER run after port mismatch; cleanup must remove only our run containers.
    uv_calls = _read_jsonl(uv_log_path)
    assert all("pytest" not in " ".join(c["argv"]) for c in uv_calls)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["containers"] == {}
    assert state["volumes"] == {}
    assert state["networks"] == {}
    assert _read_jsonl(log_path)


@pytest.mark.asyncio
async def test_c10_risk_01_case10_rejects_missing_or_incorrect_redis_owner_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeStage10Redis(
        db0={
            b"atlasrag:test:stage10b:owner": b"token-from-other-run",
            b"atlasrag:query:user": b"payload",
        }
    )
    monkeypatch.setattr(stage10_helpers.Redis, "from_url", lambda *_a, **_kw: fake)
    monkeypatch.setenv("ATLASRAG_STAGE10B_ISOLATED", "1")
    monkeypatch.setenv("ATLASRAG_STAGE10B_OWNER_TOKEN", "my-token")
    with pytest.raises(Exception):
        await stage10_helpers.clear_stage10_redis_db0("127.0.0.1", 26379)
    assert fake.unlink_calls == []
    assert b"atlasrag:query:user" in fake.db0


def test_c10_risk_01_case12_startup_failure_before_ownership_does_not_destroy_foreign_volumes(
    tmp_path: Path,
) -> None:
    bin_dir, log_path, uv_log_path, state_path = _create_fake_docker_and_uv(
        tmp_path,
        existing_volumes={
            "atlasrag-local_postgres_data": {
                "name": "atlasrag-local_postgres_data",
                "labels": {"com.docker.compose.project": "atlasrag-local"},
            }
        },
        fail_up=True,
        partial_up_services=(),
    )
    res = _run_verify_script(bin_dir, lock_dir=tmp_path / "locks")
    assert res.returncode != 0
    assert not _read_jsonl(uv_log_path)
    assert not _has_destructive_docker_call(_read_jsonl(log_path))
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert "atlasrag-local_postgres_data" in state["volumes"]


def test_c10_risk_01_case13_partial_startup_cleans_only_run_owned_resources(
    tmp_path: Path,
) -> None:
    bin_dir, _log_path, uv_log_path, state_path = _create_fake_docker_and_uv(
        tmp_path,
        existing_containers={
            "foreign-dev-postgres": {
                "Id": "foreign-dev-postgres",
                "State": {"Running": True, "Health": {"Status": "healthy"}},
                "Config": {
                    "Labels": {"com.docker.compose.project": "atlasrag-local"},
                    "Env": [],
                },
                "NetworkSettings": {"Ports": {}},
                "labels": {"com.docker.compose.project": "atlasrag-local"},
            }
        },
        existing_volumes={
            "atlasrag-local_postgres_data": {
                "name": "atlasrag-local_postgres_data",
                "labels": {"com.docker.compose.project": "atlasrag-local"},
            }
        },
        fail_up=True,
        partial_up_services=("postgres", "redis"),
    )
    res = _run_verify_script(bin_dir, lock_dir=tmp_path / "locks")
    assert res.returncode != 0
    assert not _read_jsonl(uv_log_path)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert set(state["containers"]) == {"foreign-dev-postgres"}
    assert set(state["volumes"]) == {"atlasrag-local_postgres_data"}
    assert state["networks"] == {}


def test_c10_risk_01_case14_valid_isolated_run_passes_and_cleans_up_with_evidence(
    tmp_path: Path,
) -> None:
    bin_dir, log_path, uv_log_path, state_path = _create_fake_docker_and_uv(
        tmp_path,
        existing_volumes={
            "atlasrag-local_postgres_data": {
                "name": "atlasrag-local_postgres_data",
                "labels": {"com.docker.compose.project": "atlasrag-local"},
            }
        },
    )
    res = _run_verify_script(bin_dir, lock_dir=tmp_path / "locks")
    assert res.returncode == 0, res.stderr
    uv_calls = _read_jsonl(uv_log_path)
    pytest_calls = [c for c in uv_calls if "pytest" in " ".join(c["argv"])]
    assert len(pytest_calls) == 1
    p = pytest_calls[0]
    assert p["ATLASRAG_STAGE10B_ISOLATED"] == "1"
    assert p["ATLASRAG_STAGE10B_RUN_ID"]
    assert p["ATLASRAG_STAGE10B_OWNER_TOKEN"]
    assert p["ATLASRAG_STAGE10B_COMPOSE_PROJECT"].startswith("atlasrag-stage10")
    assert p["ATLASRAG_ENV_FILE"] != str(REPOSITORY_ROOT / "deploy" / "local" / ".env")
    assert p["ATLASRAG_REDIS_URL"].endswith("/0")
    assert p["ATLASRAG_TEST_REDIS_URL"].endswith("/15")

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["containers"] == {}
    assert set(state["volumes"]) == {"atlasrag-local_postgres_data"}
    assert state["networks"] == {}
    assert "cleanup_evidence" in res.stdout or "remaining=0" in res.stdout
    assert _read_jsonl(log_path)


def test_c10_risk_01_case15_concurrent_conflicting_project_or_run_id_rejected(
    tmp_path: Path,
) -> None:
    import fcntl

    lock_dir = tmp_path / "locks"
    lock_dir.mkdir(mode=0o700)
    project_lock = lock_dir / "project-atlasrag-stage10-concurrent.lock"
    fd = os.open(project_lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        bin_dir, log_path, uv_log_path, _ = _create_fake_docker_and_uv(tmp_path)
        res = _run_verify_script(
            bin_dir,
            env_overrides={"COMPOSE_PROJECT_NAME": "atlasrag-stage10-concurrent"},
            lock_dir=lock_dir,
        )
        assert res.returncode != 0
        assert not _has_destructive_docker_call(_read_jsonl(log_path))
        assert not _read_jsonl(uv_log_path)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@pytest.mark.asyncio
async def test_c10_risk_01_atomic_lua_redis_cleanup_preserves_owner_and_rejects_mid_run_tamper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Owner-verified Redis DB 0 cleanup unlinks test keys via Lua while preserving owner key."""
    fake = _FakeStage10Redis(
        db0={
            b"atlasrag:test:stage10b:owner": b"valid-owner-tok",
            b"atlasrag:query:s1": b"q1",
            b"atlasrag:parent:r1:p1": b"p1",
        }
    )
    monkeypatch.setattr(stage10_helpers.Redis, "from_url", lambda *_a, **_kw: fake)
    monkeypatch.setenv("ATLASRAG_STAGE10B_ISOLATED", "1")
    monkeypatch.setenv("ATLASRAG_STAGE10B_OWNER_TOKEN", "valid-owner-tok")
    monkeypatch.setenv("ATLASRAG_REDIS_URL", "redis://127.0.0.1:26379/0")

    await stage10_helpers.clear_stage10_redis_db0("127.0.0.1", 26379)
    assert fake.unlink_calls == []
    assert fake.eval_calls == 1
    assert fake.db0 == {b"atlasrag:test:stage10b:owner": b"valid-owner-tok"}

    # Simulate owner key tampering between GET and Lua UNLINK.
    class TamperAfterGetRedis(_FakeStage10Redis):
        async def get(self, key: str | bytes) -> bytes | None:
            val = await super().get(key)
            self.db0[b"atlasrag:test:stage10b:owner"] = b"attacker"
            return val

    tamper_fake = TamperAfterGetRedis(
        db0={
            b"atlasrag:test:stage10b:owner": b"valid-owner-tok",
            b"atlasrag:query:s2": b"keep",
        }
    )
    monkeypatch.setattr(
        stage10_helpers.Redis, "from_url", lambda *_a, **_kw: tamper_fake
    )
    with pytest.raises(Exception, match="ownership changed"):
        await stage10_helpers.clear_stage10_redis_db0("127.0.0.1", 26379)
    assert tamper_fake.db0[b"atlasrag:query:s2"] == b"keep"


def test_c10_risk_01_milvus_drop_requires_matching_sentinel_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_milvus = _FakeStage10Milvus(
        collections={
            "atlas_chunks": {"collection_name": "atlas_chunks"},
            "atlasrag_stage10b_owner": {
                "collection_name": "atlasrag_stage10b_owner",
                "description": "atlasrag_stage10b_run_id:atlasrag_s10b_other",
            },
        }
    )
    monkeypatch.setattr(stage10_helpers, "MilvusClient", lambda **_kw: fake_milvus)
    monkeypatch.setenv("ATLASRAG_STAGE10B_ISOLATED", "1")
    monkeypatch.setenv("ATLASRAG_STAGE10B_RUN_ID", "atlasrag_s10b_0123456789abcdef")
    monkeypatch.setenv("ATLASRAG_MILVUS_URI", "http://127.0.0.1:29530")
    with pytest.raises(Exception, match="sentinel mismatch"):
        stage10_helpers.drop_stage10_milvus_collection("http://127.0.0.1:29530")
    assert fake_milvus.dropped == []

    # Matching sentinel authorizes dropping ONLY atlas_chunks.
    fake_milvus.collections["atlasrag_stage10b_owner"]["description"] = (
        "atlasrag_stage10b_run_id:atlasrag_s10b_0123456789abcdef"
    )
    stage10_helpers.drop_stage10_milvus_collection("http://127.0.0.1:29530")
    assert fake_milvus.dropped == ["atlas_chunks"]
    assert "atlasrag_stage10b_owner" in fake_milvus.collections
