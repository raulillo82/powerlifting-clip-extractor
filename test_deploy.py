"""Tests for ansible/files/powerlifting-deploy.sh against real git repositories.

podman and systemctl are replaced by stubs that log their calls.
"""
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent / "ansible" / "files" / "powerlifting-deploy.sh"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.test",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
}


def git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, text=True, capture_output=True,
        env={**os.environ, **GIT_ENV},
    ).stdout.strip()


class Host:
    def __init__(self, root: Path):
        self.origin = root / "origin"
        self.prod = root / "prod"
        self.staging = root / "staging"
        self.state = root / "state"
        self.log = root / "calls.log"
        self.bin = root / "bin"
        self.fail_build = root / "fail_build"

        self.bin.mkdir()
        for tool in ("podman", "systemctl"):
            stub = self.bin / tool
            stub.write_text(
                "#!/bin/sh\n"
                f'echo "{tool} $*" >> "{self.log}"\n'
                f'[ "$1" = build ] && [ -e "{self.fail_build}" ] && exit 1\n'
                "exit 0\n"
            )
            stub.chmod(0o755)

        self.origin.mkdir()
        git(self.origin, "init", "-q", "-b", "main")
        self.commit("app.py")
        git(self.origin, "checkout", "-q", "--orphan", "deploy-state")
        git(self.origin, "rm", "-rfq", ".")
        self.commit("staging_branch", "none\n")
        git(self.origin, "checkout", "-q", "main")
        git(self.origin, "checkout", "-q", "-b", "feature/x")
        self.commit("feature.py")
        git(self.origin, "checkout", "-q", "main")
        git(root, "clone", "-q", str(self.origin), str(self.prod))
        git(root, "clone", "-q", str(self.origin), str(self.staging))

    def commit(self, name, content="x\n", branch=None):
        if branch:
            git(self.origin, "checkout", "-q", branch)
        path = self.origin / name
        path.write_text(path.read_text() + content if path.exists() else content)
        git(self.origin, "add", ".")
        git(self.origin, "commit", "-q", "-m", name)
        sha = git(self.origin, "rev-parse", "HEAD")
        if branch:
            git(self.origin, "checkout", "-q", "main")
        return sha

    def set_staging(self, value):
        git(self.origin, "checkout", "-q", "deploy-state")
        (self.origin / "staging_branch").write_text(value + "\n")
        git(self.origin, "commit", "-q", "-am", "target")
        git(self.origin, "checkout", "-q", "main")

    def run(self, check=True):
        env = {
            **os.environ, **GIT_ENV,
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "PROD_DIR": str(self.prod), "STAGING_DIR": str(self.staging),
            "STATE_DIR": str(self.state),
        }
        result = subprocess.run(
            ["bash", str(SCRIPT)], env=env, text=True, capture_output=True,
        )
        if check:
            assert result.returncode == 0, result.stdout + result.stderr
        return result

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def builds(self):
        return [c for c in self.calls() if c.startswith("podman build")]


@pytest.fixture
def host(tmp_path):
    return Host(tmp_path)


def test_first_run_adopts_the_current_commit_without_rebuilding(host):
    host.run()
    assert host.calls() == []
    assert (host.state / "production").read_text().strip() == git(host.prod, "rev-parse", "HEAD")


def test_new_commit_on_main_is_built_once_and_restarted(host):
    host.run()
    sha = host.commit("app.py")
    host.run()
    host.run()
    assert len(host.builds()) == 1
    assert "localhost/powerlifting:latest" in host.builds()[0]
    assert "systemctl --user restart powerlifting" in host.calls()
    assert git(host.prod, "rev-parse", "HEAD") == sha


def test_documentation_only_commit_updates_without_restarting(host):
    host.run()
    sha = host.commit("README.md")
    host.run()
    assert host.calls() == []
    assert git(host.prod, "rev-parse", "HEAD") == sha


def test_failed_build_is_retried_then_left_alone_until_a_new_commit(host):
    host.run()
    host.fail_build.touch()
    host.commit("app.py")
    for _ in range(3):
        assert host.run(check=False).returncode == 1
    host.run()
    assert len(host.builds()) == 3
    assert not any("restart" in c for c in host.calls())

    host.fail_build.unlink()
    sha = host.commit("app.py")
    host.run()
    assert len(host.builds()) == 4
    assert (host.state / "production").read_text().strip() == sha
    assert not (host.state / "production.failed").exists()


def test_retry_after_failure_still_rebuilds_a_docs_only_followup(host):
    """Docs after a failed commit must not mark the broken code as deployed."""
    host.run()
    host.fail_build.touch()
    host.commit("app.py")
    host.run(check=False)
    host.fail_build.unlink()
    host.commit("README.md")
    host.run()
    assert len(host.builds()) == 2


def test_staging_follows_the_named_branch_and_its_new_commits(host):
    host.run()
    host.set_staging("feature/x")
    host.run()
    assert git(host.staging, "branch", "--show-current") == "feature/x"
    assert "localhost/powerlifting-staging:latest" in host.builds()[-1]

    sha = host.commit("feature.py", branch="feature/x")
    host.run()
    assert git(host.staging, "rev-parse", "HEAD") == sha
    assert len(host.builds()) == 2


def test_staging_follows_a_force_pushed_branch(host):
    host.set_staging("feature/x")
    host.run()
    git(host.origin, "checkout", "-q", "feature/x")
    git(host.origin, "reset", "-q", "--hard", "main")
    sha = host.commit("other.py")
    git(host.origin, "checkout", "-q", "main")
    host.run()
    assert git(host.staging, "rev-parse", "HEAD") == sha


def test_staging_none_leaves_staging_untouched(host):
    host.set_staging("feature/x")
    host.run()
    before = len(host.builds())
    host.set_staging("none")
    host.commit("feature.py", branch="feature/x")
    host.run()
    assert len(host.builds()) == before
    assert git(host.staging, "branch", "--show-current") == "feature/x"


@pytest.mark.parametrize("name", [
    "$(touch pwned)", "`touch pwned`", "-Ffoo", "a;touch pwned", "a..b",
])
def test_hostile_branch_names_are_rejected(host, name):
    host.set_staging(name)
    result = host.run(check=False)
    assert result.returncode == 1
    assert "invalid branch name" in result.stdout
    assert host.builds() == []
    assert not (host.staging / "pwned").exists()


def test_deleted_branch_is_not_an_error(host):
    host.set_staging("feature/gone")
    host.run()
    assert host.builds() == []


def test_host_without_staging_deploys_production(host):
    subprocess.run(["rm", "-rf", str(host.staging)], check=True)
    host.run()
    host.commit("app.py")
    host.run()
    assert len(host.builds()) == 1


def test_dependabot_pull_requests_do_not_select_staging():
    workflow = (SCRIPT.parent.parent.parent / ".github" / "workflows" / "staging.yml").read_text()
    assert "github.event.pull_request.user.login != 'dependabot[bot]'" in workflow
