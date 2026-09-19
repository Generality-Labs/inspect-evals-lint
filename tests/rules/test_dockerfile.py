"""The dockerfile_locking rule: parser, passing shapes, each class of unlocked input, and its option."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from inspect_evals_lint.config import PRESETS, ConfigError, LintConfig
from inspect_evals_lint.context import LintContext
from inspect_evals_lint.diagnostics import Diagnostic, Finding
from inspect_evals_lint.rules.dockerfile import (
    RULE_NAME,
    dockerfile_locking,
    host_lock_coupling,
    parse_dockerfile,
    split_shell_commands,
)
from tests.conftest import write

DIGEST = "@sha256:" + "a" * 64
COMMIT = "b" * 40

KERNELBENCH_DOCKERFILE = f"""# BUILD_CONTEXT=.
# BUILD_PLATFORMS=linux/amd64
FROM nvidia/cuda:12.9.0-devel-ubuntu24.04{DIGEST}

ENV UV_PYTHON=3.12.12
ENV UV_PROJECT_ENVIRONMENT=/workspace/venv

COPY --from=ghcr.io/astral-sh/uv:0.12.5{DIGEST} /uv /usr/local/bin/uv

WORKDIR /opt/uv-project/kernelbench
COPY src/inspect_evals/kernelbench/sandbox/pyproject.toml src/inspect_evals/kernelbench/sandbox/uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

WORKDIR /workspace
"""

DS1000_DOCKERFILE = f"""# IMAGE_NAME=ds10000
FROM python:3.10-slim{DIGEST}

WORKDIR /ds1000

COPY docker-requirements.txt .
RUN pip install --no-cache-dir -r docker-requirements.txt
"""


def _config(coupling: str | None = "warn", **options: object) -> LintConfig:
    table: dict[str, object] = dict(options)
    if coupling is not None:
        table["host_lock_coupling"] = coupling
    return replace(PRESETS["template"], rule_options={RULE_NAME: table})


def _context(root: Path, eval_path: Path, config: LintConfig) -> LintContext:
    return LintContext(
        root=root,
        name=eval_path.name,
        path=eval_path,
        kind="eval",
        config=config,
        test_path=None,
        test_search_path=None,
    )


def _run(root: Path, eval_path: Path, coupling: str = "warn") -> list[Finding]:
    return list(dockerfile_locking(_context(root, eval_path, _config(coupling))))


def _statuses(results: list[Finding]) -> list[str]:
    return [r.status for r in results]


def _text(finding: Finding) -> str:
    """Message and hint together, since the remedy lives in the hint."""
    if isinstance(finding, Diagnostic) and finding.hint:
        return f"{finding.message}; {finding.hint}"
    return finding.message


def _texts(results: list[Finding]) -> list[str]:
    return [_text(r) for r in results]


def _kernelbench_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A monorepo with a standalone sandbox project under the eval directory."""
    root = tmp_path / "repo"
    eval_path = root / "src/inspect_evals/kernelbench"
    write(root / "pyproject.toml", '[project]\nname = "inspect_evals"\n')
    write(root / "uv.lock", "version = 1\n# host lock\n")
    write(eval_path / "__init__.py", "")
    write(eval_path / "Dockerfile", KERNELBENCH_DOCKERFILE)
    write(eval_path / "sandbox/pyproject.toml", '[project]\nname = "sandbox"\n')
    write(eval_path / "sandbox/uv.lock", "version = 1\n# sandbox lock\n")
    return root, eval_path


def _single_dockerfile(
    tmp_path: Path, content: str, extra: dict[str, str] | None = None
) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    eval_path = root / "src/my_eval"
    write(root / "pyproject.toml", '[project]\nname = "x"\n')
    write(eval_path / "__init__.py", "")
    write(eval_path / "Dockerfile", content)
    for rel, body in (extra or {}).items():
        write(eval_path / rel, body)
    return root, eval_path


class TestParser:
    def test_joins_continuations_and_keeps_line_numbers(self, tmp_path: Path) -> None:
        dockerfile = write(
            tmp_path / "Dockerfile",
            "FROM scratch\n# a comment\nRUN apt-get update && \\\n    apt-get install -y curl \\\n    git\nWORKDIR /app\n",
        )
        instructions, directives = parse_dockerfile(dockerfile)
        assert [(i.keyword, i.line) for i in instructions] == [
            ("FROM", 1),
            ("RUN", 3),
            ("WORKDIR", 6),
        ]
        assert "apt-get install -y curl git" in instructions[1].args.replace("  ", " ")
        assert directives == {}

    def test_reads_directive_comments(self, tmp_path: Path) -> None:
        dockerfile = write(
            tmp_path / "Dockerfile",
            "# BUILD_CONTEXT=.\n# syntax=docker/dockerfile:1\nFROM scratch\n",
        )
        _, directives = parse_dockerfile(dockerfile)
        assert directives == {"BUILD_CONTEXT": ".", "syntax": "docker/dockerfile:1"}

    def test_captures_instruction_flags(self, tmp_path: Path) -> None:
        dockerfile = write(
            tmp_path / "Dockerfile",
            "FROM alpine AS builder\nCOPY --from=builder --chown=1:1 /a /b\n"
            "RUN --mount=type=bind,source=uv.lock,target=uv.lock uv sync --locked\n",
        )
        instructions, _ = parse_dockerfile(dockerfile)
        assert instructions[1].flags == {"from": "builder", "chown": "1:1"}
        assert instructions[1].args == "/a /b"
        assert instructions[2].flags == {"mount": "type=bind,source=uv.lock,target=uv.lock"}
        assert instructions[2].args == "uv sync --locked"

    def test_split_shell_commands_handles_operators_and_exec_form(self) -> None:
        assert split_shell_commands(
            "apt-get update && apt-get install -y curl; pip install x || true"
        ) == [
            ["apt-get", "update"],
            ["apt-get", "install", "-y", "curl"],
            ["pip", "install", "x"],
            ["true"],
        ]
        assert split_shell_commands("curl -LsSf https://astral.sh/uv/install.sh | sh") == [
            ["curl", "-LsSf", "https://astral.sh/uv/install.sh"],
            ["sh"],
        ]
        assert split_shell_commands('["sh", "-c", "pip install a && pip install b"]') == [
            ["pip", "install", "a"],
            ["pip", "install", "b"],
        ]
        assert split_shell_commands('["uv", "sync", "--locked"]') == [["uv", "sync", "--locked"]]


class TestPassingShapes:
    def test_no_dockerfile_skips(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        eval_path = root / "src/my_eval"
        write(eval_path / "__init__.py", "")
        assert _statuses(_run(root, eval_path)) == ["skip"]

    def test_kernelbench_standalone_uv_project_passes(self, tmp_path: Path) -> None:
        root, eval_path = _kernelbench_repo(tmp_path)
        results = _run(root, eval_path)
        assert _statuses(results) == ["pass"], _texts(results)

    def test_kernelbench_finding_is_unaffected_by_host_lock_changes(self, tmp_path: Path) -> None:
        """Regression for the coupling inspect_evals#2462 removed."""
        root, eval_path = _kernelbench_repo(tmp_path)
        before = [(r.status, r.message) for r in _run(root, eval_path)]
        (root / "uv.lock").write_text("version = 1\n# dependabot bumped something\n")
        after = [(r.status, r.message) for r in _run(root, eval_path)]
        assert before == after == [("pass", before[0][1])]

    def test_hashed_requirements_snapshot_passes(self, tmp_path: Path) -> None:
        root, eval_path = _single_dockerfile(
            tmp_path,
            f"FROM python:3.12-slim{DIGEST}\nCOPY requirements.txt .\n"
            "RUN pip install --require-hashes -r requirements.txt\n",
            extra={"requirements.txt": "numpy==1.26.4 --hash=sha256:" + "c" * 64 + "\n"},
        )
        assert _statuses(_run(root, eval_path)) == ["pass"]

    def test_project_install_without_resolution_passes(self, tmp_path: Path) -> None:
        root, eval_path = _single_dockerfile(
            tmp_path,
            f"FROM python:3.12-slim{DIGEST}\nCOPY requirements.txt .\n"
            "RUN pip install --require-hashes -r requirements.txt\n"
            "COPY . .\nRUN pip install . --no-deps --no-build-isolation\n",
            extra={"requirements.txt": "numpy==1.26.4 --hash=sha256:" + "c" * 64 + "\n"},
        )
        assert _statuses(_run(root, eval_path)) == ["pass"]

    def test_stage_alias_and_scratch_need_no_digest(self, tmp_path: Path) -> None:
        root, eval_path = _single_dockerfile(
            tmp_path,
            f"FROM python:3.12-slim{DIGEST} AS builder\nFROM scratch\nCOPY --from=builder /app /app\n",
        )
        assert _statuses(_run(root, eval_path)) == ["pass"]

    def test_bind_mount_provides_the_lock(self, tmp_path: Path) -> None:
        root, eval_path = _single_dockerfile(
            tmp_path,
            f"FROM python:3.12{DIGEST}\n"
            "RUN --mount=type=bind,source=uv.lock,target=uv.lock "
            "--mount=type=bind,source=pyproject.toml,target=pyproject.toml uv sync --locked\n",
            extra={"pyproject.toml": "", "uv.lock": ""},
        )
        assert _statuses(_run(root, eval_path)) == ["pass"]

    def test_git_dependency_with_full_commit_in_the_lock_is_accepted(self, tmp_path: Path) -> None:
        root, eval_path = _single_dockerfile(
            tmp_path,
            f"FROM python:3.12{DIGEST}\nCOPY pyproject.toml uv.lock ./\nRUN uv sync --locked\n",
            extra={
                "pyproject.toml": f'[project]\ndependencies = ["kb @ git+https://github.com/org/repo@{COMMIT}"]\n',
                "uv.lock": "",
            },
        )
        assert _statuses(_run(root, eval_path)) == ["pass"]


class TestUnlockedDependencyInstalls:
    def test_requirements_without_hashes_warns_about_transitives(self, tmp_path: Path) -> None:
        root, eval_path = _single_dockerfile(
            tmp_path, DS1000_DOCKERFILE, extra={"docker-requirements.txt": "numpy==1.26.4\n"}
        )
        results = _run(root, eval_path)
        assert _statuses(results) == ["warn"]
        (finding,) = results
        assert isinstance(finding, Diagnostic)
        assert "--require-hashes" in finding.message
        assert "uv sync --locked" in (finding.hint or "")
        assert finding.line == 7
        assert finding.file == eval_path / "Dockerfile"
        assert finding.severity == "warning"

    def test_bare_package_install_warns(self, tmp_path: Path) -> None:
        root, eval_path = _single_dockerfile(
            tmp_path, f"FROM python:3.12{DIGEST}\nRUN pip install numpy pandas>=2\n"
        )
        results = _run(root, eval_path)
        assert _statuses(results) == ["warn"]
        assert "numpy" in results[0].message
        assert "pandas>=2" in results[0].message

    def test_uv_sync_without_locked_warns(self, tmp_path: Path) -> None:
        root, eval_path = _single_dockerfile(
            tmp_path,
            f"FROM python:3.12{DIGEST}\nCOPY pyproject.toml uv.lock ./\nRUN uv sync\n",
            extra={"pyproject.toml": "", "uv.lock": ""},
        )
        results = _run(root, eval_path)
        assert _statuses(results) == ["warn"]
        assert "--locked" in _text(results[0])

    def test_uv_sync_frozen_warns_about_freshness(self, tmp_path: Path) -> None:
        root, eval_path = _single_dockerfile(
            tmp_path,
            f"FROM python:3.12{DIGEST}\nCOPY pyproject.toml uv.lock ./\nRUN uv sync --frozen\n",
            extra={"pyproject.toml": "", "uv.lock": ""},
        )
        results = _run(root, eval_path)
        assert _statuses(results) == ["warn"]
        assert "freshness" in results[0].message

    def test_uv_lock_during_build_warns(self, tmp_path: Path) -> None:
        root, eval_path = _single_dockerfile(
            tmp_path,
            f"FROM python:3.12{DIGEST}\nCOPY pyproject.toml ./\nRUN uv lock && uv sync --locked\n",
            extra={"pyproject.toml": ""},
        )
        messages = [r.message for r in _run(root, eval_path)]
        assert any("generates or updates the lock" in m for m in messages)
        assert any("uv.lock" in m and "not copied" in m for m in messages)

    def test_uv_sync_without_lock_in_context_warns(self, tmp_path: Path) -> None:
        root, eval_path = _single_dockerfile(
            tmp_path,
            f"FROM python:3.12{DIGEST}\nCOPY pyproject.toml ./\nRUN uv sync --locked\n",
            extra={"pyproject.toml": ""},
        )
        results = _run(root, eval_path)
        assert _statuses(results) == ["warn"]
        assert "uv.lock" in results[0].message

    def test_copy_of_missing_path_warns(self, tmp_path: Path) -> None:
        root, eval_path = _single_dockerfile(
            tmp_path,
            f"FROM python:3.12{DIGEST}\nCOPY sandbox/uv.lock sandbox/pyproject.toml ./\nRUN uv sync --locked\n",
        )
        messages = [r.message for r in _run(root, eval_path)]
        assert any("sandbox/uv.lock" in m and "build context" in m for m in messages)

    def test_project_install_with_resolution_warns(self, tmp_path: Path) -> None:
        root, eval_path = _single_dockerfile(
            tmp_path, f"FROM python:3.12{DIGEST}\nCOPY . .\nRUN pip install .\n"
        )
        results = _run(root, eval_path)
        assert _statuses(results) == ["warn"]
        assert "--no-deps" in _text(results[0])

    def test_uv_add_at_build_time_warns(self, tmp_path: Path) -> None:
        root, eval_path = _single_dockerfile(
            tmp_path, f"FROM python:3.12{DIGEST}\nRUN uv add numpy && uv tool install ruff\n"
        )
        messages = [r.message for r in _run(root, eval_path)]
        assert messages == [
            "uv add resolves dependencies at build time",
            "uv tool resolves dependencies at build time",
        ]

    def test_templated_package_name_is_reported_as_unverified(self, tmp_path: Path) -> None:
        root, eval_path = _single_dockerfile(
            tmp_path, f"FROM python:3.12{DIGEST}\nRUN pip3 install {{pip3_installs}}\n"
        )
        results = _run(root, eval_path)
        assert _statuses(results) == ["warn"]
        assert "cannot be verified" in results[0].message


class TestMutableImageAndSourceInputs:
    def test_base_image_without_digest_warns(self, tmp_path: Path) -> None:
        root, eval_path = _single_dockerfile(tmp_path, "FROM python:3.10-slim\n")
        results = _run(root, eval_path)
        assert _statuses(results) == ["warn"]
        (finding,) = results
        assert isinstance(finding, Diagnostic)
        assert "python:3.10-slim" in finding.message
        assert "digest" in finding.message
        assert "@sha256:<digest>" in (finding.hint or "")
        assert finding.line == 1

    def test_copy_from_external_image_without_digest_warns(self, tmp_path: Path) -> None:
        root, eval_path = _single_dockerfile(
            tmp_path,
            f"FROM python:3.12{DIGEST}\nCOPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv\n",
        )
        results = _run(root, eval_path)
        assert _statuses(results) == ["warn"]
        assert "ghcr.io/astral-sh/uv:0.12.5" in results[0].message

    def test_dynamic_base_image_is_unverified_not_passing(self, tmp_path: Path) -> None:
        root, eval_path = _single_dockerfile(tmp_path, "ARG BASE=python:3.12\nFROM ${BASE}\n")
        results = _run(root, eval_path)
        assert _statuses(results) == ["warn"]
        assert "cannot be verified" in results[0].message

    def test_git_dependency_without_full_commit_warns(self, tmp_path: Path) -> None:
        root, eval_path = _single_dockerfile(
            tmp_path,
            f"FROM python:3.12{DIGEST}\n"
            "RUN pip install --require-hashes -r r.txt git+https://github.com/org/repo@main\n",
            extra={"r.txt": ""},
        )
        messages = [r.message for r in _run(root, eval_path)]
        assert any("full commit" in m for m in messages)

    def test_git_clone_without_commit_warns(self, tmp_path: Path) -> None:
        root, eval_path = _single_dockerfile(
            tmp_path,
            f"FROM python:3.12{DIGEST}\nRUN git clone https://github.com/org/repo /opt/repo\n",
        )
        results = _run(root, eval_path)
        assert _statuses(results) == ["warn"]
        assert "git clone" in results[0].message

    def test_installer_script_piped_to_shell_warns(self, tmp_path: Path) -> None:
        root, eval_path = _single_dockerfile(
            tmp_path,
            f"FROM python:3.12{DIGEST}\nRUN curl -LsSf https://astral.sh/uv/install.sh | sh\n",
        )
        results = _run(root, eval_path)
        assert _statuses(results) == ["warn"]
        assert "integrity" in results[0].message

    def test_package_manager_bootstrap_without_hashes_warns(self, tmp_path: Path) -> None:
        root, eval_path = _single_dockerfile(
            tmp_path, f"FROM python:3.12{DIGEST}\nRUN pip install --upgrade pip uv==0.12.5\n"
        )
        results = _run(root, eval_path)
        assert _statuses(results) == ["warn"]
        (finding,) = results
        assert isinstance(finding, Diagnostic)
        assert "uv==0.12.5" in finding.message
        assert "COPY --from" in (finding.hint or "")

    def test_unsupported_package_manager_is_unverified(self, tmp_path: Path) -> None:
        root, eval_path = _single_dockerfile(
            tmp_path, f"FROM node:22{DIGEST}\nRUN npm install -g some-tool\n"
        )
        results = _run(root, eval_path)
        assert _statuses(results) == ["warn"]
        assert "npm" in results[0].message

    def test_remote_add_source_is_unverified(self, tmp_path: Path) -> None:
        root, eval_path = _single_dockerfile(
            tmp_path, f"FROM python:3.12{DIGEST}\nADD https://example.com/tool.tar.gz /opt/\n"
        )
        results = _run(root, eval_path)
        assert _statuses(results) == ["warn"]
        assert "remote" in results[0].message


class TestVisibleLimits:
    def test_os_package_install_is_a_visible_limit_not_a_warning(self, tmp_path: Path) -> None:
        root, eval_path = _single_dockerfile(
            tmp_path,
            f"FROM ubuntu:24.04{DIGEST}\nRUN apt-get update && apt-get install -y --no-install-recommends git\n",
        )
        results = _run(root, eval_path)
        assert _statuses(results) == ["pass"]
        assert "OS package installation (apt-get)" in results[0].message
        assert "not lockable" in results[0].message

    def test_kernelbench_with_apt_passes_and_notes_the_os_limit(self, tmp_path: Path) -> None:
        root, eval_path = _kernelbench_repo(tmp_path)
        dockerfile = eval_path / "Dockerfile"
        dockerfile.write_text(
            dockerfile.read_text().replace(
                "ENV UV_PYTHON=3.12.12\n",
                "ENV UV_PYTHON=3.12.12\nRUN apt-get update && apt-get install -y --no-install-recommends git\n",
            )
        )
        results = _run(root, eval_path)
        assert _statuses(results) == ["pass"]
        assert "OS package installation" in results[0].message

    def test_os_limit_is_not_reported_alongside_real_findings(self, tmp_path: Path) -> None:
        root, eval_path = _single_dockerfile(
            tmp_path, "FROM ubuntu:24.04\nRUN apt-get update && apt-get install -y git\n"
        )
        results = _run(root, eval_path)
        assert _statuses(results) == ["warn"]
        assert "digest" in results[0].message


class TestHostLockCoupling:
    def test_warns_in_a_monorepo(self, tmp_path: Path) -> None:
        root, eval_path = _kernelbench_repo(tmp_path)
        (eval_path / "Dockerfile").write_text(
            KERNELBENCH_DOCKERFILE.replace(
                "COPY src/inspect_evals/kernelbench/sandbox/pyproject.toml "
                "src/inspect_evals/kernelbench/sandbox/uv.lock ./",
                "COPY pyproject.toml uv.lock ./",
            )
        )
        results = _run(root, eval_path, coupling="warn")
        assert _statuses(results) == ["warn"]
        (finding,) = results
        assert isinstance(finding, Diagnostic)
        assert "host project" in finding.message
        assert 'host-lock-coupling = "allow"' in (finding.hint or "")

    def test_allowed_for_a_standalone_repo(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        eval_path = root / "src/my_eval"
        write(root / "pyproject.toml", '[project]\nname = "my-eval"\n')
        write(root / "uv.lock", "")
        write(eval_path / "__init__.py", "")
        write(
            eval_path / "Dockerfile",
            f"# BUILD_CONTEXT=.\nFROM python:3.12{DIGEST}\nCOPY pyproject.toml uv.lock ./\nRUN uv sync --locked\n",
        )
        assert _statuses(_run(root, eval_path, coupling="allow")) == ["pass"]

    def test_default_is_allow_and_the_monorepo_preset_warns(self) -> None:
        assert host_lock_coupling({}) == "allow"
        assert host_lock_coupling(PRESETS["template"].rule_options.get(RULE_NAME, {})) == "allow"
        assert host_lock_coupling(PRESETS["register"].rule_options.get(RULE_NAME, {})) == "allow"
        assert host_lock_coupling(PRESETS["monorepo"].rule_options[RULE_NAME]) == "warn"

    @pytest.mark.parametrize(
        ("options", "match"),
        [
            ({"host_lock_coupling": "maybe"}, "must be one of"),
            ({"host_lock_coupling": True}, "must be one of"),
            ({"host_lock_coupling": "warn", "strict": True}, "unknown option"),
        ],
    )
    def test_invalid_options_are_configuration_errors(
        self, tmp_path: Path, options: dict[str, object], match: str
    ) -> None:
        root, eval_path = _single_dockerfile(tmp_path, "FROM scratch\n")
        config = _config(None, **options)
        with pytest.raises(ConfigError, match=match):
            list(dockerfile_locking(_context(root, eval_path, config)))


class TestParserEdgeCasesFromRealRepositories:
    def test_heredoc_copy_and_run_are_not_treated_as_sources(self, tmp_path: Path) -> None:
        root, eval_path = _single_dockerfile(
            tmp_path,
            f"FROM python:3.12{DIGEST}\n"
            "COPY <<EOF /app/config.txt\nFROM nowhere\npip install evil\nEOF\n"
            "RUN <<'SCRIPT'\napt-get update\nSCRIPT\n",
        )
        results = _run(root, eval_path)
        assert _statuses(results) == ["pass"], _texts(results)

    def test_identical_findings_on_one_instruction_are_reported_once(self, tmp_path: Path) -> None:
        root, eval_path = _single_dockerfile(
            tmp_path, f"FROM python:3.12{DIGEST}\nRUN pip install numpy && pip install numpy\n"
        )
        assert _statuses(_run(root, eval_path)) == ["warn"]

    def test_compose_build_context_is_used_for_copy_sources(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        eval_path = root / "src/my_eval"
        write(root / "pyproject.toml", '[project]\nname = "x"\n')
        write(eval_path / "__init__.py", "")
        write(
            eval_path / "compose.yaml",
            "services:\n  default:\n    build:\n      context: ./images/worker\n      dockerfile: Dockerfile\n",
        )
        write(
            eval_path / "images/worker/Dockerfile",
            f"FROM python:3.12{DIGEST}\nCOPY requirements.txt .\n"
            "RUN pip install --require-hashes -r requirements.txt\n",
        )
        write(
            eval_path / "images/worker/requirements.txt",
            "numpy==1.26.4 --hash=sha256:" + "c" * 64 + "\n",
        )
        assert _statuses(_run(root, eval_path)) == ["pass"]

    def test_compose_build_context_elsewhere_resolves_sources_there(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        eval_path = root / "src/my_eval"
        write(root / "pyproject.toml", '[project]\nname = "x"\n')
        write(eval_path / "__init__.py", "")
        write(
            eval_path / "compose.yaml",
            "services:\n  default:\n    build:\n      context: ./data\n      dockerfile: ../docker/Dockerfile\n",
        )
        write(
            eval_path / "docker/Dockerfile",
            f"FROM python:3.12{DIGEST}\nCOPY certs/nginx.crt /etc/nginx.crt\n",
        )
        write(eval_path / "data/certs/nginx.crt", "cert")
        assert _statuses(_run(root, eval_path)) == ["pass"]

    def test_build_context_outside_the_repository_is_one_warning(self, tmp_path: Path) -> None:
        root, eval_path = _single_dockerfile(
            tmp_path, f"# BUILD_CONTEXT=../elsewhere\nFROM python:3.12{DIGEST}\nCOPY x .\n"
        )
        results = _run(root, eval_path)
        assert _statuses(results) == ["warn"]
        assert "outside the repository" in results[0].message

    def test_excluded_dockerfiles_are_not_read(self, tmp_path: Path) -> None:
        root, eval_path = _single_dockerfile(
            tmp_path,
            f"FROM python:3.12{DIGEST}\n",
            extra={"challenges/x/Dockerfile": "FROM python:3.12\n"},
        )
        assert _statuses(_run(root, eval_path)) == ["warn"]
        config = replace(_config("allow"), exclude=("src/my_eval/challenges/**",))
        results = list(dockerfile_locking(_context(root, eval_path, config)))
        assert _statuses(results) == ["pass"]
