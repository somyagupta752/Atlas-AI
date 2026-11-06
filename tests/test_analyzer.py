from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.code_intelligence import (
    RepositoryNotFoundError,
    analyze_repository,
    analyze_source,
)
from app.code_intelligence.schemas import ErrorType, FileStatus
from app.config import Settings
from app.main import app, get_allowed_repository_roots


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "sample_project"
    _write(repo, "main.py", "from services.user import UserService\n\nUserService().run()\n")
    _write(
        repo,
        "services/user.py",
        "class UserService(BaseService):\n    def run(self):\n        return self.load()\n",
    )
    _write(repo, "services/broken.py", "def broken(:\n    pass\n")
    _write(repo, ".venv/lib/ignored.py", "x = 1\n")
    _write(repo, "README.md", "# docs\n")
    return repo


# --- analyzer -------------------------------------------------------------


def test_analyzes_multiple_files_and_isolates_the_invalid_one(sample_repo):
    result = analyze_repository(sample_repo)

    assert result.total_files == 3
    assert result.successful_files == 2
    assert result.failed_files == 1
    assert [f.file_path for f in result.files] == [
        "main.py",
        "services/broken.py",
        "services/user.py",
    ]

    by_path = {f.file_path: f for f in result.files}
    broken = by_path["services/broken.py"]
    assert broken.status == FileStatus.ERROR
    [error] = broken.errors
    assert error.error_type == ErrorType.SYNTAX_ERROR
    assert error.file_path == "services/broken.py"
    assert error.line == 1

    user = by_path["services/user.py"]
    assert user.status == FileStatus.SUCCESS
    assert user.classes[0].methods == ["run"]
    assert user.inheritance[0].base == "BaseService"
    assert by_path["main.py"].imports[0].module == "services.user"


def test_empty_repository(tmp_path):
    result = analyze_repository(tmp_path)

    assert (result.total_files, result.successful_files, result.failed_files) == (0, 0, 0)
    assert result.files == []


def test_missing_repository_raises(tmp_path):
    with pytest.raises(RepositoryNotFoundError):
        analyze_repository(tmp_path / "missing")


def test_unreadable_file_is_a_read_error(tmp_path):
    _write(tmp_path, "good.py", "x = 1\n")
    (tmp_path / "bad.py").write_bytes(b"x = '\xff'\n")

    result = analyze_repository(tmp_path)

    bad = next(f for f in result.files if f.file_path == "bad.py")
    assert bad.errors[0].error_type == ErrorType.DECODE_ERROR
    assert result.successful_files == 1


def test_output_is_deterministic(sample_repo):
    assert analyze_repository(sample_repo) == analyze_repository(sample_repo)


def test_extraction_failure_becomes_structured_error(monkeypatch):
    def explode(tree, file_path):
        raise RecursionError("too deep")

    monkeypatch.setattr("app.code_intelligence.analyzer.extract_structure", explode)

    parsed = analyze_source("x = 1\n", "deep.py")

    assert parsed.status == FileStatus.ERROR
    assert parsed.errors[0].error_type == ErrorType.ANALYSIS_ERROR


def test_analysis_never_executes_repository_code(tmp_path):
    repo = tmp_path / "repo"
    marker = tmp_path / "executed.marker"
    payload = f"open({str(marker)!r}, 'w').write('ran')"
    _write(
        repo,
        "evil.py",
        f"""
import os
{payload}

def side_effect(arg=({payload})):
    {payload}

@({payload} or (lambda f: f))
class Evil:
    value = ({payload})
""",
    )
    # A module the evil file imports: importing it would also create the marker.
    _write(repo, "os.py", payload + "\n")

    result = analyze_repository(repo)

    assert result.successful_files == 2
    assert not marker.exists()


# --- API ------------------------------------------------------------------


@pytest.fixture
def api(tmp_path):
    # Every test repository lives under tmp_path, so allow exactly that root.
    app.dependency_overrides[get_allowed_repository_roots] = lambda: [tmp_path]
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_api_analyzes_valid_repository(api, sample_repo):
    response = api.post("/repositories/analyze", json={"repository_path": str(sample_repo)})

    assert response.status_code == 200
    body = response.json()
    assert body["repository_path"] == str(sample_repo.resolve())
    assert (body["total_files"], body["successful_files"], body["failed_files"]) == (3, 2, 1)
    user = next(f for f in body["files"] if f["file_path"] == "services/user.py")
    assert user["status"] == "success"
    assert user["classes"][0]["name"] == "UserService"


def test_api_returns_structured_syntax_errors(api, sample_repo):
    body = api.post("/repositories/analyze", json={"repository_path": str(sample_repo)}).json()

    broken = next(f for f in body["files"] if f["file_path"] == "services/broken.py")
    assert broken["status"] == "error"
    assert broken["errors"][0] == {
        "error_type": "syntax_error",
        "message": broken["errors"][0]["message"],
        "file_path": "services/broken.py",
        "line": 1,
        "column": broken["errors"][0]["column"],
    }


def test_api_missing_repository_is_404(api, tmp_path):
    response = api.post(
        "/repositories/analyze", json={"repository_path": str(tmp_path / "nope")}
    )

    assert response.status_code == 404


def test_api_file_path_is_400(api, tmp_path):
    _write(tmp_path, "file.py", "")

    response = api.post(
        "/repositories/analyze", json={"repository_path": str(tmp_path / "file.py")}
    )

    assert response.status_code == 400


@pytest.mark.parametrize("payload", [{}, {"repository_path": ""}, {"repository_path": "rel/p"}])
def test_api_rejects_invalid_input(api, payload):
    assert api.post("/repositories/analyze", json=payload).status_code == 422


def test_api_enforces_allowed_roots(api, tmp_path, sample_repo):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    _write(allowed, "ok.py", "x = 1\n")
    app.dependency_overrides[get_allowed_repository_roots] = lambda: [allowed]

    outside = api.post("/repositories/analyze", json={"repository_path": str(sample_repo)})
    escape = api.post(
        "/repositories/analyze", json={"repository_path": f"{allowed}/../sample_project"}
    )
    inside = api.post("/repositories/analyze", json={"repository_path": str(allowed)})

    assert outside.status_code == 403
    assert escape.status_code == 403
    assert inside.status_code == 200


def test_allowlist_defaults_to_empty_when_unset(monkeypatch):
    monkeypatch.delenv("ALLOWED_REPOSITORY_ROOTS", raising=False)

    settings = Settings(_env_file=None, database_url="postgresql://unused")

    assert settings.allowed_repository_roots == []


def _no_allowed_roots() -> list[Path]:
    return []


def test_api_fails_closed_with_empty_allowlist(api, sample_repo):
    app.dependency_overrides[get_allowed_repository_roots] = _no_allowed_roots

    response = api.post("/repositories/analyze", json={"repository_path": str(sample_repo)})

    assert response.status_code == 403
    assert "not configured" in response.json()["detail"]


def test_api_rejects_before_revealing_whether_path_exists(api, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    app.dependency_overrides[get_allowed_repository_roots] = lambda: [allowed]

    response = api.post(
        "/repositories/analyze", json={"repository_path": str(tmp_path / "missing")}
    )

    assert response.status_code == 403


def test_api_rejects_symlink_escaping_allowed_root(api, tmp_path, sample_repo):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (allowed / "link").symlink_to(sample_repo, target_is_directory=True)
    app.dependency_overrides[get_allowed_repository_roots] = lambda: [allowed]

    response = api.post("/repositories/analyze", json={"repository_path": str(allowed / "link")})

    assert response.status_code == 403


def test_api_accepts_symlinked_allowed_root(api, tmp_path, sample_repo):
    alias = tmp_path / "alias"
    alias.symlink_to(sample_repo, target_is_directory=True)
    app.dependency_overrides[get_allowed_repository_roots] = lambda: [alias]

    response = api.post("/repositories/analyze", json={"repository_path": str(sample_repo)})

    assert response.status_code == 200
