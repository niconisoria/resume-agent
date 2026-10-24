import re
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "ci.yml"
APP_PATHS = ["app/", "Dockerfile", "fly.toml", "pyproject.toml", "uv.lock"]
EVAL_PATHS = ["app/", "evals/", "pyproject.toml", "uv.lock"]
EVAL_SECRETS = [
    "CI_ANTHROPIC_API_KEY",
    "VOYAGE_API_KEY",
    "DATABASE_URL",
    "PHOENIX_COLLECTOR_ENDPOINT",
    "PHOENIX_API_KEY",
]


def load():
    wf = yaml.safe_load(WORKFLOW.read_text())
    # PyYAML reads the bare `on` key as boolean True.
    wf["on"] = wf.pop(True, wf.get("on"))
    return wf


def job(name):
    return load()["jobs"][name]


def run_text(j):
    return "\n".join(step.get("run", "") for step in j["steps"])


def test_runs_on_every_pr_and_push_to_main():
    on = load()["on"]
    assert "pull_request" in on
    assert on["push"]["branches"] == ["main"]


def test_check_job_runs_lint_format_and_tests():
    script = run_text(job("check"))
    for cmd in ["uv sync --frozen", "ruff check", "ruff format --check", "pytest"]:
        assert cmd in script


def test_check_job_needs_no_secrets():
    assert "secrets." not in yaml.safe_dump(job("check"))


# --- change detection ----------------------------------------------------------


def test_changes_job_detects_app_and_eval_paths_for_prs_and_pushes():
    changes = job("changes")
    script = run_text(changes)
    for path in set(APP_PATHS) | set(EVAL_PATHS):
        assert path in script
    assert {"app", "eval"} <= set(changes["outputs"])
    assert "github.event.pull_request.base.sha" in yaml.safe_dump(changes)
    assert "github.event.before" in yaml.safe_dump(changes)


def test_eval_paths_exclude_docs():
    script = run_text(job("changes"))
    assert "docs/" not in script


# --- eval gate ------------------------------------------------------------------


def test_eval_runs_after_checks_only_when_eval_paths_change():
    ev = job("eval")
    assert set(ev["needs"]) == {"check", "changes"}
    assert "needs.changes.outputs.eval == 'true'" in ev["if"]
    assert ev["timeout-minutes"] <= 30


def test_eval_uses_ci_anthropic_key_and_dev_project():
    ev = job("eval")
    env = {}
    for step in ev["steps"]:
        env |= step.get("env", {})
    env |= ev.get("env", {})
    assert env["ANTHROPIC_API_KEY"] == "${{ secrets.CI_ANTHROPIC_API_KEY }}"
    assert env["PHOENIX_PROJECT_NAME"] == "resume-agent-dev"
    for name in [
        "VOYAGE_API_KEY",
        "DATABASE_URL",
        "PHOENIX_COLLECTOR_ENDPOINT",
        "PHOENIX_API_KEY",
    ]:
        assert env[name] == f"${{{{ secrets.{name} }}}}"
    assert "python -m evals.run_evals" in run_text(ev)


def test_missing_secrets_fail_loudly_before_eval():
    ev = job("eval")
    guard = next(s for s in ev["steps"] if s.get("name") == "Require eval secrets")
    assert "::error::" in guard["run"] and "exit 1" in guard["run"]
    names = [s.get("name") for s in ev["steps"]]
    assert names.index("Require eval secrets") < names.index("Run eval suite")


def test_report_always_in_job_summary():
    step = next(
        s for s in job("eval")["steps"] if s.get("name") == "Report to job summary"
    )
    assert step["if"] == "always()"
    assert "GITHUB_STEP_SUMMARY" in step["run"] and "evals/REPORT.md" in step["run"]


def test_pr_gets_one_sticky_comment():
    ev = job("eval")
    assert ev["permissions"]["pull-requests"] == "write"
    step = next(s for s in ev["steps"] if s.get("name") == "Sticky PR comment")
    assert "pull_request" in step["if"] and "always()" in step["if"]
    assert "<!-- eval-report -->" in step["run"]
    assert "PATCH" in step["run"]  # updates existing comment instead of adding new ones


# --- deploy -------------------------------------------------------------------


def test_deploy_waits_for_eval_and_only_runs_on_main_push_with_app_changes():
    deploy = job("deploy")
    assert set(deploy["needs"]) == {"check", "changes", "eval"}
    cond = deploy["if"]
    for part in [
        "always()",
        "github.event_name == 'push'",
        "refs/heads/main",
        "needs.check.result == 'success'",
        "needs.changes.outputs.app == 'true'",
        "needs.eval.result == 'success'",
        "needs.eval.result == 'skipped'",
    ]:
        assert part in cond


def test_deploy_keeps_single_machine_and_smoke_checks():
    script = run_text(job("deploy"))
    assert "--ha=false" in script
    assert "/health" in script
    assert "machine list" in script


def test_deploys_never_overlap_and_newest_wins():
    concurrency = job("deploy")["concurrency"]
    assert concurrency["group"]
    assert concurrency["cancel-in-progress"] is False


def test_fly_token_only_on_flyctl_steps():
    deploy = job("deploy")
    assert "FLY_API_TOKEN" not in deploy.get("env", {})
    for step in deploy["steps"]:
        if "FLY_API_TOKEN" in step.get("env", {}):
            assert "flyctl" in step["run"]


# --- secrets hygiene -------------------------------------------------------------


def test_secrets_only_via_secrets_context():
    text = WORKFLOW.read_text()
    assert "FlyV1" not in text and "sk-ant-" not in text
    for name in EVAL_SECRETS + ["FLY_API_TOKEN"]:
        assert f"${{{{ secrets.{name} }}}}" in text


def test_actions_pinned_to_versions():
    uses = re.findall(r"uses:\s*(\S+)", WORKFLOW.read_text())
    assert uses
    assert all(re.search(r"@v?\d", u) for u in uses)


# --- review fixes ---------------------------------------------------------------


def step(job_name, step_name):
    return next(s for s in job(job_name)["steps"] if s.get("name") == step_name)


def test_knowledge_changes_do_not_trigger_eval():
    script = step("changes", "Detect changed areas")["run"]
    eval_case = next(line for line in script.splitlines() if "eval=true ;;" in line)
    assert "knowledge/" not in eval_case


def test_pr_diff_uses_pr_head_and_null_separated_paths():
    s = step("changes", "Detect changed areas")
    assert "github.event.pull_request.head.sha" in s["env"]["HEAD"]
    assert "--name-only -z" in s["run"] and "read -r -d ''" in s["run"]


def test_stale_report_cleared_before_anything_can_fail():
    names = [s.get("name") for s in job("eval")["steps"]]
    assert names.index("Clear stale report") < names.index("Require eval secrets")
    assert "evals/REPORT.md" in step("eval", "Clear stale report")["run"]


def test_sticky_comment_only_touches_own_bot_comment_and_skips_forks():
    s = step("eval", "Sticky PR comment")
    assert "github-actions[bot]" in s["run"]
    assert "head.repo.full_name == github.repository" in s["if"]


def test_placeholders_scoped_to_eval_step_not_job():
    assert "GITHUB_TOKEN" not in job("eval").get("env", {})
    assert step("eval", "Run eval suite")["env"]["GITHUB_TOKEN"] == "ci-placeholder"


def test_superseded_commit_never_deploys():
    steps = job("deploy")["steps"]
    names = [s.get("name") for s in steps]
    guard = names.index("Skip if superseded")
    assert "commits/main" in steps[guard]["run"]
    for s in steps[guard + 1 :]:
        assert s["if"] == "steps.latest.outputs.deploy == 'true'"
