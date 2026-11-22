"""fix_docker_compose_companions — the companion files exist and agree
with docker/docker-compose.yml, so one command really brings the
stack up.

Before this fix the compose file was correct but unbuildable:
docker/ had no Dockerfile at all (`docker compose build` failed on
Dockerfile.api), src/scheduler/ had no __main__.py (the scheduler
container's `python -m src.scheduler --beat` died instantly),
scripts/ had no init_secrets.sh (`docker compose config` failed on
the missing ../.secrets/*.txt files), and .gitignore covered neither
.secrets/ nor .env. The old compose test only checked compose TEXT,
so all of that stayed green — these tests check the FILES.

Stdlib-only except the optional YAML parse and the optional real
`docker compose config` (both skip when the tool is absent, like the
sibling test_fix_docker_compose_unrunnable).

Windows CI note: the init_secrets test runs the script through the
bash found on PATH (Git Bash/MSYS on the Windows runner). That bash
cannot open a backslash Windows path (C:\\Users\\...) — the first CI
run failed with "/bin/bash: C:\\...\\init_secrets.sh: No such file or
directory" (exit 127) — so the test converts the path to POSIX form
(/c/Users/...) and writes the script with LF line endings regardless
of the checkout's autocrlf setting. The chmod-600 mode assertion is
POSIX-only: Windows filesystems do not carry POSIX mode bits.
Run from repo root:  python -m pytest tests/test_fix_docker_compose_companions.py -q
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE = REPO_ROOT / "docker" / "docker-compose.yml"
DOCKERFILES = {
    "api": REPO_ROOT / "docker" / "Dockerfile.api",
    "worker": REPO_ROOT / "docker" / "Dockerfile.worker",
    "frontend": REPO_ROOT / "docker" / "Dockerfile.frontend",
}
SCHEDULER_MAIN = REPO_ROOT / "src" / "scheduler" / "__main__.py"
WORKER_MAIN = REPO_ROOT / "src" / "worker" / "__main__.py"
INIT_SECRETS = REPO_ROOT / "scripts" / "init_secrets.sh"
SECRET_NAMES = ("postgres_password", "vault_master_key", "vault_backup_passphrase")


# --- Existence: the whole point of the fix ---------------------------------
def test_all_companion_files_exist():
    for path in (*DOCKERFILES.values(), SCHEDULER_MAIN, WORKER_MAIN, INIT_SECRETS):
        assert path.is_file(), f"companion file missing: {path.relative_to(REPO_ROOT)}"


def test_every_compose_build_points_at_a_real_dockerfile():
    yaml = pytest.importorskip("yaml", reason="pyyaml chua co o moi truong nay")
    parsed = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    for svc in ("api", "worker", "scheduler", "frontend"):
        build = parsed["services"][svc]["build"]
        target = REPO_ROOT / build["dockerfile"]
        assert target.is_file(), f"{svc}: {build['dockerfile']} does not exist"


# --- Dockerfiles: multi-stage, non-root, real CMDs ---------------------------
@pytest.mark.parametrize("name", ["api", "worker", "frontend"])
def test_dockerfile_is_multistage_and_nonroot(name):
    text = DOCKERFILES[name].read_text(encoding="utf-8")
    assert text.count("FROM ") >= 2, f"Dockerfile.{name} is not multi-stage"
    user_lines = [ln.split(None, 1)[1].strip() for ln in text.splitlines()
                  if ln.startswith("USER ")]
    assert user_lines and all(u not in ("root", "0") for u in user_lines), (
        f"Dockerfile.{name} must run as a non-root USER")


def test_dockerfile_api_cmd_is_the_locked_uvicorn_command():
    text = DOCKERFILES["api"].read_text(encoding="utf-8")
    assert 'CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]' in text


def test_dockerfile_worker_cmd_runs_the_real_worker_loop():
    text = DOCKERFILES["worker"].read_text(encoding="utf-8")
    assert 'CMD ["python", "-m", "src.worker"]' in text
    # The module that CMD names must exist (a CMD pointing at a missing
    # module is exactly the dead-command bug class of this fix).
    assert WORKER_MAIN.is_file()


def test_dockerfile_frontend_builds_src_web_with_npm():
    text = DOCKERFILES["frontend"].read_text(encoding="utf-8")
    assert "node:" in text and "npm run build" in text and "npm" in text
    assert "src/web" in text


# --- Scheduler / worker entrypoints are real --------------------------------
def test_scheduler_main_is_a_real_beat_entrypoint():
    source = SCHEDULER_MAIN.read_text(encoding="utf-8")
    assert "schedule_jobs_for_account" in source
    assert "enqueue" in source
    assert "--beat" in source
    assert "SIGTERM" in source  # graceful shutdown, not a bare while-True


def test_worker_main_uses_the_scheduler_worker_api():
    source = WORKER_MAIN.read_text(encoding="utf-8")
    assert "acquire_job" in source and "finish_job" in source
    assert "SIGTERM" in source


def _run_module(args, env_extra, timeout=60):
    env = dict(os.environ)
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", *args], cwd=str(REPO_ROOT), env=env,
        capture_output=True, text=True, timeout=timeout)


def test_scheduler_beat_once_with_real_accounts_exits_clean(tmp_path):
    accounts = [
        {"id": "acc-ok", "workspace_id": "ws1", "platform": "youtube",
         "platform_user_id": "u1", "display_name": "OK", "credential_ref": "cred:1",
         "status": "active",
         "videos": [{"video_id": "v1", "is_new": True}]},
        {"id": "acc-banned", "workspace_id": "ws1", "platform": "tiktok",
         "platform_user_id": "u2", "display_name": "Banned", "credential_ref": "cred:2",
         "status": "banned", "videos": []},
    ]
    accounts_file = tmp_path / "accounts.json"
    accounts_file.write_text(json.dumps(accounts), encoding="utf-8")
    result = _run_module(["src.scheduler", "--beat", "--once"],
                         {"SCHEDULER_ACCOUNTS_FILE": str(accounts_file)})
    # The banned account's jobs are REJECTED by the fail-closed gate and
    # counted — the beat itself must still exit 0, not crash.
    assert result.returncode == 0, result.stderr
    assert "tick done" in result.stderr


def test_scheduler_beat_once_without_accounts_is_a_clean_noop():
    result = _run_module(["src.scheduler", "--beat", "--once"],
                         {"SCHEDULER_ACCOUNTS_FILE": ""})
    assert result.returncode == 0, result.stderr


def test_worker_once_without_executor_idles_and_exits_clean():
    result = _run_module(["src.worker", "--once"], {"WORKER_EXECUTOR": ""})
    assert result.returncode == 0, result.stderr


# --- init_secrets.sh really creates what compose expects ---------------------
def _bash_path(path: Path) -> str:
    """Path in the form the bash on PATH understands.

    POSIX: unchanged. Windows (Git Bash/MSYS): C:\\Users\\x\\f.sh and
    even C:/Users/x/f.sh are not openable by that bash — it wants the
    MSYS form /c/Users/x/f.sh.
    """
    s = path.as_posix()
    if os.name == "nt" and re.match(r"^[A-Za-z]:/", s):
        s = "/" + s[0].lower() + s[2:]
    return s


def _run_init_secrets(script: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", _bash_path(script)],
        capture_output=True, text=True, timeout=60)


def test_init_secrets_creates_all_secret_files_mode_600(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("bash chua co o moi truong nay")
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    # Write the script with LF endings explicitly: a Windows checkout
    # with core.autocrlf=true would otherwise hand bash a CRLF script.
    script = scripts_dir / "init_secrets.sh"
    script.write_text(
        INIT_SECRETS.read_bytes().decode("utf-8").replace("\r\n", "\n"),
        encoding="utf-8", newline="\n")
    result = _run_init_secrets(script)
    assert result.returncode == 0, result.stderr
    secrets_dir = tmp_path / ".secrets"
    first_contents = {}
    for name in SECRET_NAMES:
        target = secrets_dir / f"{name}.txt"
        assert target.is_file() and target.read_text(encoding="utf-8").strip()
        if os.name != "nt":
            # POSIX mode bits only exist on POSIX filesystems; on
            # Windows the chmod in the script is a no-op by design.
            assert stat.S_IMODE(target.stat().st_mode) == 0o600
        first_contents[name] = target.read_text(encoding="utf-8")
    # Re-running must NOT rotate existing secrets (rotating the vault
    # master key would orphan every encrypted credential).
    result2 = _run_init_secrets(script)
    assert result2.returncode == 0, result2.stderr
    for name in SECRET_NAMES:
        assert (secrets_dir / f"{name}.txt").read_text(encoding="utf-8") == first_contents[name]


def test_init_secrets_script_references_the_compose_secret_names():
    text = INIT_SECRETS.read_text(encoding="utf-8")
    for name in SECRET_NAMES:
        assert name in text
    assert "600" in text


# --- .gitignore keeps secrets and env out of the repo ------------------------
def test_gitignore_covers_secrets_dir_and_env_file():
    lines = {ln.strip() for ln in
             (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()}
    assert ".secrets/" in lines
    assert ".env" in lines


# --- Real compose validation when docker is available ------------------------
def test_docker_compose_config_validates_with_companions():
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker chua co o moi truong nay")
    secrets_dir = REPO_ROOT / ".secrets"
    created_dir = not secrets_dir.exists()
    secrets_dir.mkdir(exist_ok=True)
    created_files: list[Path] = []
    try:
        for name in SECRET_NAMES:
            target = secrets_dir / f"{name}.txt"
            if not target.exists():
                target.write_text("dev-only-placeholder", encoding="utf-8")
                created_files.append(target)
        env = dict(os.environ)
        env["COMPOSE_PROJECT_NAME"] = "test-compose-companions"
        result = subprocess.run(
            [docker, "compose", "-f", str(COMPOSE), "config", "--quiet"],
            capture_output=True, text=True, env=env, cwd=str(REPO_ROOT))
        assert result.returncode == 0, result.stderr
    finally:
        for target in created_files:
            target.unlink(missing_ok=True)
        if created_dir:
            secrets_dir.rmdir()
