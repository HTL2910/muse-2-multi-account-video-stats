"""fix_docker_compose_unrunnable — compose chay duoc (phan thuoc ve compose).

Da sua trong docker/docker-compose.yml:
- api healthcheck probe /healthz -> /health (FastAPI chi co GET /health).
- scheduler command chet `python -m src.scheduler.task_scheduler --beat`
  -> `python -m src.scheduler --beat` (entrypoint that, __main__.py).
- backup: pg_dump khong doc PGPASSWORD_FILE -> lay password tu secret
  file vao PGPASSWORD trong entrypoint.
- Ghi ro companion files bat buoc o buoc merge (Dockerfiles, __main__.py,
  scripts/init_secrets.sh, .gitignore) vi nhanh fix/* chi duoc nop
  compose + test nay.

Test chu yeu dung stdlib (doc text); phan parse YAML va `docker compose
config` chi chay khi moi truong co san pyyaml / docker, neu khong thi skip.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE = REPO_ROOT / "docker" / "docker-compose.yml"


@pytest.fixture(scope="module")
def text() -> str:
    return COMPOSE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def parsed():
    yaml = pytest.importorskip("yaml", reason="pyyaml chua co o moi truong nay")
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def test_compose_file_exists():
    assert COMPOSE.is_file()


def test_api_healthcheck_probes_real_endpoint(text):
    assert "/healthz" not in text
    assert "http://localhost:8000/health" in text


def test_scheduler_uses_real_entrypoint_not_dead_module(text):
    assert "src.scheduler.task_scheduler" not in text
    assert '"python", "-m", "src.scheduler", "--beat"' in text


def test_scheduler_has_graceful_stop_and_single_replica(parsed):
    sched = parsed["services"]["scheduler"]
    assert sched["command"] == ["python", "-m", "src.scheduler", "--beat"]
    assert sched["deploy"]["replicas"] == 1
    assert sched["stop_grace_period"] == "30s"
    assert "SCHEDULER_INTERVAL_SECONDS" in str(sched.get("environment", {}))


def test_all_builds_reference_dockerfiles(parsed):
    for svc in ("api", "worker", "scheduler", "frontend"):
        build = parsed["services"][svc]["build"]
        assert build["context"] == ".."
        assert build["dockerfile"].startswith("docker/Dockerfile.")
    assert parsed["services"]["api"]["build"]["dockerfile"] == "docker/Dockerfile.api"
    assert parsed["services"]["worker"]["build"]["dockerfile"] == "docker/Dockerfile.worker"
    assert parsed["services"]["frontend"]["build"]["dockerfile"] == "docker/Dockerfile.frontend"


def test_backup_reads_postgres_password_from_secret_file(text):
    # pg_dump khong doc PGPASSWORD_FILE; phai export tu /run/secrets.
    assert "PGPASSWORD=$(cat /run/secrets/postgres_password)" in text
    assert "PGPASSWORD_FILE" not in text


def test_secrets_still_file_based_and_documented(text):
    for name in ("postgres_password", "vault_master_key", "vault_backup_passphrase"):
        assert f"../.secrets/{name}.txt" in text
    assert "scripts/init_secrets.sh" in text


def test_companion_files_listed_as_merge_prerequisites(text):
    # Nhung file nay KHONG the nop o nhanh nay; compose phai ghi ro de
    # buoc merge khong quen — neu khong `up` van that bai.
    for required in (
        "docker/Dockerfile.api",
        "docker/Dockerfile.worker",
        "docker/Dockerfile.frontend",
        "src/scheduler/__main__.py",
        "scripts/init_secrets.sh",
        ".secrets/",
        ".env",
    ):
        assert required in text, f"thieu companion file trong header: {required}"


def test_services_and_volumes_structure(parsed):
    services = parsed["services"]
    for name in ("postgres", "redis", "api", "worker", "scheduler", "frontend",
                 "backup", "vault-backup"):
        assert name in services
    for name in ("postgres_data", "redis_data", "db_backups", "vault_key_backups"):
        assert name in parsed["volumes"]
    # api/worker/scheduler chi khoi dong khi DB + Redis healthy.
    for name in ("api", "worker", "scheduler"):
        deps = services[name]["depends_on"]
        assert deps["postgres"]["condition"] == "service_healthy"
        assert deps["redis"]["condition"] == "service_healthy"
    # Worker mac dinh 2 replica, scale duoc; scheduler dung 1 replica.
    assert services["worker"]["deploy"]["replicas"] == "${WORKER_REPLICAS:-2}"
    assert services["scheduler"]["deploy"]["replicas"] == 1


def test_docker_compose_config_validates():
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker chua co o moi truong nay")
    # Secret file duoc resolve tuong doi theo thu muc chua compose file
    # (docker/../.secrets = goc repo): tao file gia tam thoi o do, chi
    # nhung file nao chua co, va xoa dung nhung file minh tao o finally.
    secrets_dir = REPO_ROOT / ".secrets"
    created_dir = not secrets_dir.exists()
    secrets_dir.mkdir(exist_ok=True)
    created_files: list[Path] = []
    try:
        for name in ("postgres_password", "vault_master_key", "vault_backup_passphrase"):
            target = secrets_dir / f"{name}.txt"
            if not target.exists():
                target.write_text("dev-only-placeholder", encoding="utf-8")
                created_files.append(target)
        env = dict(os.environ)
        env["COMPOSE_PROJECT_NAME"] = "test-compose-fix"
        result = subprocess.run(
            [docker, "compose", "-f", str(COMPOSE), "config", "--quiet"],
            capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0, result.stderr
    finally:
        for target in created_files:
            target.unlink(missing_ok=True)
        if created_dir:
            secrets_dir.rmdir()
