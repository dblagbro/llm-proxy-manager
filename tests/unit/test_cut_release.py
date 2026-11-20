import io
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

import pytest


def test_release_build_and_backup_match_commit():
    real_git = shutil.which("git")
    if real_git is None:
        pytest.skip("git is required for the isolated release fixture")
    with tempfile.TemporaryDirectory(prefix="llm-proxy-release-fixture-") as td:
        root = Path(td)
        repo, bin_dir, backups = root / "repo", root / "bin", root / "backups"
        for d in (repo / "tools", repo / "app", bin_dir, backups):
            d.mkdir(parents=True, exist_ok=True)
        script = (Path(__file__).resolve().parents[2] / "tools/cut-release.sh").read_text()
        (repo / "tools/cut-release.sh").write_text(script)
        (repo / "app/__version__.py").write_text('__version__ = "9.9.9"\n')
        (repo / "README.md").write_text("Current version: **v9.9.9**\n")
        (repo / "marker").write_text("reviewed commit\n")
        executable_marker = repo / "executable-marker"
        executable_marker.write_text("#!/bin/sh\nexit 0\n")
        executable_marker.chmod(0o755)

        def executable(name, text):
            p = bin_dir / name
            p.write_text("#!/bin/sh\n" + text + "\n")
            p.chmod(0o755)

        executable(
            "gh",
            'if [ "$1" = repo ]; then echo fixture/repo; exit 0; fi; '
            'if [ "$1" = api ]; then echo HTTP ${GH_STATUS:-404} >&2; exit 1; fi; exit 0',
        )
        executable(
            "curl",
            'case "$*" in *hub.docker.com*/tags/*) printf ${TAG_STATUS:-404};; '
            "*hub.docker.com*) printf 200;; "
            '*two.example.invalid*) if [ "${FAIL_SECOND_HEALTH:-0}" = 1 ]; then '
            'printf "{\\"status\\":\\"broken\\"}\\n"; else '
            'printf "{\\"status\\":\\"healthy\\"}\\n"; fi;; '
            '*) printf "{\\"status\\":\\"healthy\\",\\"version\\":\\"1\\"}\\n";; esac',
        )
        executable(
            "git",
            f'if [ "$1" = push ] || [ "$1" = ls-remote ]; then exit 0; fi; exec {real_git} "$@"',
        )
        executable(
            "sudo",
            'if [ "$1" = docker ] && [ "$2" = image ] && [ "$3" = inspect ]; then\n'
            '  printf "sha256:%064d\\n" 0; exit 0; fi\n'
            'if [ "$1" = docker ] && [ "$2" = tag ]; then exit 9; fi\n'
            'if [ "$1" = docker ] && [ "$2" = build ]; then\n'
            '  [ "${FAIL_BUILD:-0}" = 1 ] && exit 42\n'
            '  eval "last=\\${$#}"\n'
            '  test "$(cat "$last/marker")" = "reviewed commit" || exit 2\n'
            '  if [ "${CHECK_ARCHIVE_MODE:-0}" = 1 ]; then\n'
            '    test "$(stat -c %a "$last/marker")" = "$ARCHIVED_FILE_MODE" || exit 3\n'
            '    test "$(stat -c %a "$last/executable-marker")" = "$ARCHIVED_EXEC_MODE" || exit 4\n'
            "  fi\n"
            "fi\nexit 0",
        )
        env = os.environ.copy()
        env.update(
            PATH=str(bin_dir) + ":" + env["PATH"],
            GIT_AUTHOR_NAME="Fixture",
            GIT_AUTHOR_EMAIL="fixture@example.invalid",
            GIT_COMMITTER_NAME="Fixture",
            GIT_COMMITTER_EMAIL="fixture@example.invalid",
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL="/dev/null",
        )

        # Never fall through to real publishing commands when the temporary
        # fixture directory is mounted noexec.
        try:
            subprocess.run([str(bin_dir / "gh"), "auth", "status"], env=env, check=True)
        except PermissionError:
            pytest.skip("release fixture requires an executable temporary directory")

        def run(*args):
            return subprocess.run(
                args, cwd=repo, env=env, check=True, text=True, capture_output=True
            )

        run(real_git, "init", "-q", "-b", "main")
        run(real_git, "add", ".")
        run(real_git, "commit", "-qm", "fixture")
        commit = run(real_git, "rev-parse", "HEAD").stdout.strip()
        archive_bytes = subprocess.run(
            [real_git, "archive", "--format=tar", commit],
            cwd=repo,
            env=env,
            check=True,
            capture_output=True,
        ).stdout
        with tarfile.open(fileobj=io.BytesIO(archive_bytes)) as archive:
            env["ARCHIVED_FILE_MODE"] = format(archive.getmember("marker").mode, "o")
            env["ARCHIVED_EXEC_MODE"] = format(archive.getmember("executable-marker").mode, "o")
        required = subprocess.run(
            [
                "bash",
                "tools/cut-release.sh",
                "--docker-repo",
                "fixture/repo",
                "--tested-image-id",
                "sha256:" + "0" * 64,
            ],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert required.returncode != 0 and "--backup-dir" in required.stderr
        missing_health = subprocess.run(
            [
                "bash",
                "tools/cut-release.sh",
                "--docker-repo",
                "fixture/repo",
                "--tested-image-id",
                "sha256:" + "0" * 64,
                "--backup-dir",
                str(backups),
            ],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert missing_health.returncode != 0 and "--health-url" in missing_health.stderr
        symlink_backup = root / "linked-backups"
        symlink_backup.symlink_to(backups, target_is_directory=True)
        invalid_backup = subprocess.run(
            [
                "bash",
                "tools/cut-release.sh",
                "--docker-repo",
                "fixture/repo",
                "--tested-image-id",
                "sha256:" + "0" * 64,
                "--backup-dir",
                str(symlink_backup),
                "--health-url",
                "https://one.example.invalid/health",
            ],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert invalid_backup.returncode != 0 and "--backup-dir" in invalid_backup.stderr
        bad_url = subprocess.run(
            [
                "bash",
                "tools/cut-release.sh",
                "--docker-repo",
                "fixture/repo",
                "--tested-image-id",
                "sha256:" + "0" * 64,
                "--backup-dir",
                str(backups),
                "--health-url",
                "https://user:pw@example.invalid/health",
            ],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert bad_url.returncode != 0 and "health URLs" in bad_url.stderr
        bad_health = subprocess.run(
            [
                "bash",
                "tools/cut-release.sh",
                "--docker-repo",
                "fixture/repo",
                "--tested-image-id",
                "sha256:" + "0" * 64,
                "--backup-dir",
                str(backups),
                "--health-url",
                "https://one.example.invalid/health",
                "--health-url",
                "https://two.example.invalid/health",
            ],
            cwd=repo,
            env=dict(env, FAIL_SECOND_HEALTH="1"),
            capture_output=True,
            text=True,
            check=False,
        )
        assert bad_health.returncode != 0 and "live verification failed" in bad_health.stderr
        mismatch = subprocess.run(
            [
                "bash",
                "tools/cut-release.sh",
                "--docker-repo",
                "fixture/repo",
                "--tested-image-id",
                "sha256:" + "0" * 64,
                "--backup-dir",
                str(backups),
                "--health-url",
                "https://one.example.invalid/health",
                "--health-url",
                "https://two.example.invalid/health",
                "--version",
                "9.9.8",
            ],
            cwd=repo,
            check=False,
            env=env,
            capture_output=True,
            text=True,
        )
        assert mismatch.returncode != 0 and "requested version differs" in mismatch.stderr
        broken_env = dict(env, GH_STATUS="500")
        api_error = subprocess.run(
            [
                "bash",
                "tools/cut-release.sh",
                "--docker-repo",
                "fixture/repo",
                "--tested-image-id",
                "sha256:" + "0" * 64,
                "--backup-dir",
                str(backups),
                "--health-url",
                "https://one.example.invalid/health",
                "--health-url",
                "https://two.example.invalid/health",
            ],
            cwd=repo,
            check=False,
            env=broken_env,
            capture_output=True,
            text=True,
        )
        assert api_error.returncode != 0 and "could not verify" in api_error.stderr
        broken_env = dict(env, FAIL_BUILD="1")
        build_error = subprocess.run(
            [
                "bash",
                "tools/cut-release.sh",
                "--docker-repo",
                "fixture/repo",
                "--tested-image-id",
                "sha256:" + "0" * 64,
                "--backup-dir",
                str(backups),
                "--health-url",
                "https://one.example.invalid/health",
                "--health-url",
                "https://two.example.invalid/health",
            ],
            cwd=repo,
            check=False,
            env=broken_env,
            capture_output=True,
            text=True,
        )
        assert build_error.returncode != 0
        assert (
            subprocess.run(
                [real_git, "rev-parse", "-q", "--verify", "refs/tags/v9.9.9"],
                cwd=repo,
                check=False,
                capture_output=True,
            ).returncode
            != 0
        )
        tag_env = dict(env, TAG_STATUS="200")
        existing = subprocess.run(
            [
                "bash",
                "tools/cut-release.sh",
                "--docker-repo",
                "fixture/repo",
                "--tested-image-id",
                "sha256:" + "0" * 64,
                "--backup-dir",
                str(backups),
                "--health-url",
                "https://one.example.invalid/health",
                "--health-url",
                "https://two.example.invalid/health",
            ],
            cwd=repo,
            check=False,
            env=tag_env,
            capture_output=True,
            text=True,
        )
        assert existing.returncode != 0 and "tag exists" in existing.stderr
        different = subprocess.run(
            [
                "bash",
                "tools/cut-release.sh",
                "--docker-repo",
                "fixture/repo",
                "--tested-image-id",
                "sha256:" + "1" * 64,
                "--backup-dir",
                str(backups),
                "--health-url",
                "https://one.example.invalid/health",
                "--health-url",
                "https://two.example.invalid/health",
            ],
            cwd=repo,
            check=False,
            env=env,
            capture_output=True,
            text=True,
        )
        assert different.returncode != 0 and "differs from the tested image" in different.stderr
        subprocess.run(
            [
                "bash",
                "tools/cut-release.sh",
                "--docker-repo",
                "fixture/repo",
                "--tested-image-id",
                "sha256:" + "0" * 64,
                "--backup-dir",
                str(backups),
                "--health-url",
                "https://one.example.invalid/health",
                "--health-url",
                "https://two.example.invalid/health",
            ],
            cwd=repo,
            env=dict(env, CHECK_ARCHIVE_MODE="1"),
            check=True,
            text=True,
            capture_output=True,
            umask=0o077,
        )
        tag = run(real_git, "rev-parse", "v9.9.9^{}").stdout.strip()
        assert tag == commit
        archives = list(backups.glob("*.tar.gz"))
        assert len(archives) == 1
        with tarfile.open(archives[0]) as archive:
            assert archive.extractfile("llm-proxy-v2/marker").read() == b"reviewed commit\n"
