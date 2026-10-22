import re
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "ci.yml"
APP_PATHS = ["app/", "Dockerfile", "fly.toml", "pyproject.toml", "uv.lock"]


def load():
    wf = yaml.safe_load(WORKFLOW.read_text())
    # PyYAML reads the bare `on` key as boolean True.
    wf["on"] = wf.pop(True, wf.get("on"))
    return wf


def run_text(job):
    return "\n".join(step.get("run", "") for step in job["steps"])


def test_runs_on_every_pr_and_push_to_main():
    on = load()["on"]
    assert "pull_request" in on
    assert on["push"]["branches"] == ["main"]


def test_check_job_runs_lint_format_and_tests():
    script = run_text(load()["jobs"]["check"])
    for cmd in ["uv sync --frozen", "ruff check", "ruff format --check", "pytest"]:
        assert cmd in script


def test_deploy_waits_for_checks_and_only_runs_on_main_push():
    deploy = load()["jobs"]["deploy"]
    assert deploy["needs"] == "check"
    assert "github.event_name == 'push'" in deploy["if"]
    assert "refs/heads/main" in deploy["if"]


def test_deploy_skipped_unless_app_files_changed():
    script = run_text(load()["jobs"]["deploy"])
    for path in APP_PATHS:
        assert path in script


def test_deploy_keeps_single_machine_and_smoke_checks():
    script = run_text(load()["jobs"]["deploy"])
    assert "--ha=false" in script
    assert "/health" in script
    assert "machine list" in script


def test_deploys_never_overlap_and_newest_wins():
    concurrency = load()["jobs"]["deploy"]["concurrency"]
    assert concurrency["group"]
    assert concurrency["cancel-in-progress"] is False


def test_fly_token_only_from_secrets():
    text = WORKFLOW.read_text()
    assert "${{ secrets.FLY_API_TOKEN }}" in text
    assert "FlyV1" not in text


def test_check_job_needs_no_secrets():
    assert "secrets." not in yaml.safe_dump(load()["jobs"]["check"])


def test_token_only_on_flyctl_steps():
    deploy = load()["jobs"]["deploy"]
    assert "FLY_API_TOKEN" not in deploy.get("env", {})
    for step in deploy["steps"]:
        if "FLY_API_TOKEN" in step.get("env", {}):
            assert "flyctl" in step["run"]


def test_docs_only_push_skips_every_deploy_step():
    steps = load()["jobs"]["deploy"]["steps"]
    detect = next(i for i, s in enumerate(steps) if s.get("id") == "changes")
    for step in steps[detect + 1 :]:
        assert step["if"] == "steps.changes.outputs.deploy == 'true'"


def test_actions_pinned_to_versions():
    uses = re.findall(r"uses:\s*(\S+)", WORKFLOW.read_text())
    assert uses
    assert all(re.search(r"@v?\d", u) for u in uses)
