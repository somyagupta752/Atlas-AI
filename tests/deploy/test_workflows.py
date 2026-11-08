"""15.7: the CI/CD workflows follow the rules this project set: validate on PRs, never deploy, never hold secrets."""

import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
FILES = sorted(WORKFLOWS.glob("*.yml"))


def load(path: Path) -> dict:
    data = yaml.safe_load(path.read_text())
    data["on"] = data.pop(True, data.get("on"))  # YAML reads the key `on` as the boolean True
    return data


def all_steps(workflow: dict):
    for name, job in workflow["jobs"].items():
        for step in job.get("steps", []):
            yield name, step


def test_the_expected_workflows_exist():
    assert {p.name for p in FILES} == {"ci.yml", "security.yml", "codeql.yml", "build-images.yml"}


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
class TestEveryWorkflow:
    def test_permissions_are_declared_and_read_only_by_default(self, path):
        workflow = load(path)
        assert workflow["permissions"] == {"contents": "read"}

    def test_every_job_has_a_timeout(self, path):
        for name, job in load(path)["jobs"].items():
            assert 1 <= job["timeout-minutes"] <= 60, name

    def test_forks_cannot_reach_secrets(self, path):
        assert not {"pull_request_target", "workflow_run"} & set(load(path)["on"])

    def test_no_secret_other_than_the_read_only_token_is_used(self, path):
        used = set(re.findall(r"secrets\.([A-Za-z0-9_]+)", path.read_text()))
        assert used <= {"GITHUB_TOKEN"}, used

    def test_there_is_no_deployment_step(self, path):
        text = re.sub(r"(?m)^\s*#.*$", "", path.read_text())  # comments may explain what is NOT done
        for forbidden in (r"\bssh\b", r"\bscp\b", r"\brsync\b", r"kubectl", r"\bhelm\b", r"compose[^\n]* up\b",
                          r"aws-actions", r"azure/", r"google-github-actions", r"\bgcloud\b", r"\bterraform\b",
                          r"^\s*environment:", r"deploy\w*:"):
            assert not re.search(forbidden, text, re.MULTILINE), f"{path.name}: matches {forbidden!r}"

    def test_actions_are_pinned_to_a_released_version_never_a_branch(self, path):
        for _job, step in all_steps(load(path)):
            uses = step.get("uses")
            if uses and not uses.startswith("./"):
                ref = uses.split("@", 1)[1] if "@" in uses else ""
                assert re.fullmatch(r"v?\d+(\.\d+){0,2}|[0-9a-f]{40}", ref), uses

    def test_untrusted_expressions_never_enter_a_shell_script(self, path):
        for job, step in all_steps(load(path)):
            script = step.get("run", "")
            for expression in re.findall(r"\$\{\{\s*([^}]+?)\s*\}\}", script):
                assert not re.match(r"(github\.event|github\.head_ref|inputs\.|github\.ref_name)", expression), \
                    f"{path.name}/{job}: {{{{ {expression} }}}} in a run step; pass it through env instead"

    def test_checkouts_do_not_keep_credentials(self, path):
        for _job, step in all_steps(load(path)):
            if step.get("uses", "").startswith("actions/checkout"):
                assert step["with"]["persist-credentials"] is False


@pytest.mark.parametrize("name", ["ci.yml", "security.yml", "codeql.yml"])
def test_validation_runs_on_every_pull_request(name):
    assert "pull_request" in load(WORKFLOWS / name)["on"]


def test_the_release_build_is_manual_only_and_publishing_is_opt_in():
    workflow = load(WORKFLOWS / "build-images.yml")
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert workflow["on"]["workflow_dispatch"]["inputs"]["push"]["default"] is False
    pushes = [(job, step) for job, step in all_steps(workflow) if "push" in step.get("run", "")]
    assert pushes and all("inputs.push" in step.get("if", "") for _job, step in pushes)
    for name in ("ci.yml", "security.yml", "codeql.yml"):
        text = (WORKFLOWS / name).read_text()
        assert "docker push" not in text and "packages: write" not in text and "push: true" not in text, name


def test_pull_request_checks_cover_what_the_task_requires():
    ci = (WORKFLOWS / "ci.yml").read_text()
    for expected in ("ruff check", "pytest", "npm test", "npm run build", "hadolint", "docker/build-push-action",
                     "alembic check", "compose -f docker-compose.prod.yml config"):
        assert expected in ci, expected
    security = (WORKFLOWS / "security.yml").read_text()
    for expected in ("pip-audit", "npm audit", "gitleaks", "trivy", "schedule:"):
        assert expected in security.lower() or expected in security, expected


def test_ci_runs_the_browser_suite_against_a_throwaway_database():
    ci = load(WORKFLOWS / "ci.yml")
    e2e = ci["jobs"]["e2e"]
    text = json.dumps(e2e)
    assert "npx playwright test" in text and "playwright install --with-deps chromium" in text
    assert "services" in e2e and set(e2e["services"]) == {"postgres"}  # no Neo4j, no Docker workers, no model
    assert "if" not in e2e  # it runs on every pull request, not only on demand
    assert "failure()" in text and "upload-artifact" in text  # a failing run leaves a report and traces to look at
    harness = (ROOT / "frontend" / "e2e" / "support" / "run_api.py").read_text()
    assert 'PREFIX = "atlas_e2e_"' in harness and "checked_name" in harness  # the development database cannot be touched


def test_the_container_tests_cannot_be_skipped_in_ci():
    backend = load(WORKFLOWS / "ci.yml")["jobs"]["backend"]
    assert backend["env"]["REQUIRE_CONTAINER"] == "1"
    assert "worker_host" in json.dumps(backend)  # the rootless-VM tests are excluded explicitly, not forgotten


def test_tool_versions_match_the_dockerfiles():
    uv_dockerfile = re.search(r"UV_IMAGE=ghcr.io/astral-sh/uv:([\d.]+)", (ROOT / "Dockerfile").read_text()).group(1)
    node_dockerfile = re.search(r"NODE_IMAGE=node:([\d.]+)-alpine", (ROOT / "frontend" / "Dockerfile").read_text()).group(1)
    for name in ("ci.yml", "security.yml"):
        env = load(WORKFLOWS / name)["env"]
        assert env["UV_VERSION"] == uv_dockerfile, name
        assert env["NODE_VERSION"] == node_dockerfile, name


def test_ci_runs_scripts_that_exist():
    scripts = json.loads((ROOT / "frontend" / "package.json").read_text())["scripts"]
    assert {"test", "build"} <= set(scripts)
    for needed in (".hadolint.yaml", ".gitleaks.toml", ".trivyignore", ".python-version", "uv.lock",
                   "frontend/package-lock.json", "docker/test-runner.Dockerfile"):
        assert (ROOT / needed).is_file(), needed


def test_dependabot_covers_every_ecosystem():
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text())
    covered = {(u["package-ecosystem"], u["directory"]) for u in config["updates"]}
    assert {("github-actions", "/"), ("uv", "/"), ("npm", "/frontend"), ("docker", "/"), ("docker", "/frontend"),
            ("docker", "/docker")} <= covered
