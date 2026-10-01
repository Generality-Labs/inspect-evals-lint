"""README rules: commands in the README agree with the package."""

from pathlib import Path

from inspect_evals_lint import lint_package
from inspect_evals_lint.rules import readme as readme_module
from inspect_evals_lint.rules.readme import (
    readme_commands,
    readme_dependency_groups,
    readme_task_args,
)
from tests.conftest import context_for, make_monorepo, make_register_repo, write

TASKS = """
from inspect_ai import task


@task
def alpha(message_limit: int = 50, *, sandbox_type: str = "docker"):
    ...


@task
def alpha_hard(message_limit: int = 50, difficulty: str = "hard"):
    ...


@task
def alpha_open(**kwargs):
    ...
"""


def _run(tmp_path: Path, readme: str):
    eval_dir = tmp_path / "alpha"
    write(eval_dir / "__init__.py", "")
    write(eval_dir / "alpha.py", TASKS)
    write(eval_dir / "README.md", readme)
    return list(readme_task_args(context_for(eval_dir)))


def _fence(*lines: str) -> str:
    return "\n".join(["# Alpha", "", "```bash", *lines, "```", ""])


class TestReadmeCommands:
    def test_joins_continuations_and_keeps_each_word_on_its_line(self, tmp_path):
        readme = write(
            tmp_path / "README.md",
            _fence(
                "uv run inspect eval alpha/alpha \\", "    -T message_limit=5 \\", "    --limit 3"
            ),
        )
        (command,) = readme_commands(readme)
        assert [w.text for w in command][:5] == ["uv", "run", "inspect", "eval", "alpha/alpha"]
        assert [(w.text, w.line) for w in command if "=" in w.text] == [("message_limit=5", 5)]

    def test_splits_at_separators_and_reads_inline_code(self, tmp_path):
        readme = write(
            tmp_path / "README.md",
            "Run `inspect eval alpha/alpha -T x=1` here.\n" + _fence("cd x && uv sync"),
        )
        assert [[w.text for w in c] for c in readme_commands(readme)] == [
            ["inspect", "eval", "alpha/alpha", "-T", "x=1"],
            ["cd", "x"],
            ["uv", "sync"],
        ]

    def test_a_quote_spanning_lines_is_one_command(self, tmp_path):
        readme = write(
            tmp_path / "README.md",
            _fence("inspect eval alpha/alpha -T 'x={", '  "a": 1}\' --limit 1', "uv sync"),
        )
        commands = readme_commands(readme)
        assert [c[-1].text for c in commands] == ["1", "sync"]

    def test_an_unclosed_quote_costs_only_its_own_line(self, tmp_path):
        readme = write(tmp_path / "README.md", _fence("echo 'oops", "uv sync"))
        assert [[w.text for w in c] for c in readme_commands(readme)] == [["uv", "sync"]]

    def test_unspaced_separators_split_commands(self, tmp_path):
        readme = write(tmp_path / "README.md", _fence("a -T x=1;b -T y=2&&c|d"))
        assert [[w.text for w in c] for c in readme_commands(readme)] == [
            ["a", "-T", "x=1"],
            ["b", "-T", "y=2"],
            ["c"],
            ["d"],
        ]

    def test_a_stray_apostrophe_in_a_long_block_stays_linear(self, tmp_path, monkeypatch):
        lines = ["It's open", *(f'line {i} "quoted" text' for i in range(3000))]
        readme = write(tmp_path / "README.md", "```text\n" + "\n".join(lines) + "\n```\n")
        scanned = 0
        split = readme_module._split

        def counting_split(text: str):
            nonlocal scanned
            scanned += len(text)
            return split(text)

        monkeypatch.setattr(readme_module, "_split", counting_split)
        assert len(readme_commands(readme)) == 3000
        # Re-reading an ever-longer joined block is quadratic: ~3000 x 100 kB here.
        assert scanned < 50 * len(readme.read_text())


class TestReadmeTaskArgs:
    def test_skips_without_readme(self, tmp_path):
        eval_dir = tmp_path / "alpha"
        write(eval_dir / "__init__.py", "")
        assert [r.status for r in readme_task_args(context_for(eval_dir))] == ["skip"]

    def test_skips_when_no_command_passes_task_arguments(self, tmp_path):
        results = _run(tmp_path, _fence("uv run inspect eval alpha/alpha --limit 10"))
        assert [r.status for r in results] == ["skip"]

    def test_passes_when_every_argument_is_a_parameter(self, tmp_path):
        readme = _fence(
            "uv run inspect eval alpha/alpha -T message_limit=5 -Tsandbox_type=k8s",
            "inspect eval alpha_hard \\",
            "  --model openai/gpt-4o \\",
            "  -T difficulty=easy",
            "inspect eval alpha/alpha.py@alpha -T message_limit=3",
        )
        results = _run(tmp_path, readme + "Or `inspect eval alpha/alpha -T message_limit=9`.\n")
        assert [r.status for r in results] == ["pass"]
        assert "5 -T argument(s)" in results[0].message

    def test_fails_at_each_unknown_argument(self, tmp_path):
        readme = _fence(
            "uv run inspect eval alpha/alpha -T max_messages=75",
            "inspect eval alpha/alpha_hard \\",
            "  --model openai/gpt-4o \\",
            "  -T temperature=0.5",
        )
        results = _run(tmp_path, readme)
        assert [(r.status, r.line) for r in results] == [("fail", 4), ("fail", 7)]
        assert "max_messages" in results[0].message
        assert "alpha()" in results[0].message
        assert "message_limit, sandbox_type" in (results[0].hint or "")
        assert "alpha_hard()" in results[1].message

    def test_unspaced_separators_end_a_command(self, tmp_path):
        results = _run(
            tmp_path,
            _fence(
                "inspect eval alpha/alpha -T message_limit=1; inspect eval alpha_hard -T difficulty=x",
                "inspect eval alpha/alpha -T message_limit=1&&inspect eval alpha_hard -T difficulty=x",
            ),
        )
        assert [r.status for r in results] == ["pass"]

    def test_skips_a_readme_that_is_not_utf8(self, tmp_path):
        eval_dir = tmp_path / "alpha"
        write(eval_dir / "__init__.py", "")
        (eval_dir / "README.md").write_bytes(b"# Latin-1 \xe9\n")
        results = list(readme_task_args(context_for(eval_dir)))
        assert [r.status for r in results] == ["skip"]
        assert "Could not read README.md" in results[0].message

    def test_every_task_in_a_command_must_take_the_argument(self, tmp_path):
        results = _run(
            tmp_path,
            _fence("inspect eval-set alpha/alpha alpha/alpha_hard -T difficulty=easy --log-dir x"),
        )
        assert [(r.status, r.line) for r in results] == [("fail", 4)]
        assert "to alpha()" in results[0].message

    def test_resolves_a_file_path_from_the_root_or_the_readme(self, tmp_path):
        results = _run(
            tmp_path,
            _fence(
                "inspect eval alpha/alpha.py@alpha_hard -T sandbox_type=k8s",
                "inspect eval alpha.py@alpha -T difficulty=easy",
            ),
        )
        assert [(r.status, r.line) for r in results] == [("fail", 4), ("fail", 5)]

    def test_ignores_tasks_it_cannot_resolve_or_that_take_kwargs(self, tmp_path):
        results = _run(
            tmp_path,
            _fence(
                "inspect eval other/alpha -T anything=1",
                "inspect eval alpha/alpha_XX -T anything=1",
                "inspect eval alpha/alpha_open -T anything=1",
                "inspect eval src/alpha -T anything=1",
                "inspect eval alpha/other.py@alpha -T anything=1",
                "inspect eval --model m alpha/alpha -T anything=1",
            ),
        )
        assert [r.status for r in results] == ["skip"]

    def test_monorepo_namespace(self, tmp_path):
        config = make_monorepo(tmp_path)
        write(
            config.package_dir(tmp_path, "alpha") / "README.md",
            _fence("uv run inspect eval inspect_evals/alpha -T solver_name=x"),
        )
        report = lint_package(tmp_path, "alpha", config, check="readme_task_args")
        assert [(d.status, d.line) for d in report.diagnostics] == [("fail", 4)]

    def test_standalone_repo_reads_the_root_readme(self, tmp_path):
        config = make_register_repo(tmp_path)
        write(
            tmp_path / "README.md",
            _fence("uv run inspect eval alpha/alpha -T scorer=x -T solver_name=x"),
        )
        report = lint_package(tmp_path, "alpha", config, check="readme_task_args")
        assert [(d.status, d.line) for d in report.diagnostics] == [("fail", 4)]
        assert "solver_name" in report.diagnostics[0].message


PYPROJECT = """
[project]
name = "my_evals"

[project.optional-dependencies]
alpha = ["requests"]
bfcl_v4 = ["numpy"]

[dependency-groups]
dev = ["pytest"]
"""


def _deps(tmp_path: Path, readme: str, pyproject: str = PYPROJECT):
    eval_dir = tmp_path / "alpha"
    write(eval_dir / "__init__.py", "")
    write(eval_dir / "README.md", readme)
    if pyproject:
        write(tmp_path / "pyproject.toml", pyproject)
    return list(readme_dependency_groups(context_for(eval_dir)))


class TestReadmeDependencyGroups:
    def test_skips_without_readme_pyproject_or_references(self, tmp_path):
        eval_dir = tmp_path / "alpha"
        write(eval_dir / "__init__.py", "")
        assert [r.status for r in readme_dependency_groups(context_for(eval_dir))] == ["skip"]
        assert [r.status for r in _deps(tmp_path, _fence("uv sync --extra x"), "")] == ["skip"]
        assert [r.status for r in _deps(tmp_path, _fence("uv sync"))] == ["skip"]

    def test_passes_when_every_name_exists(self, tmp_path):
        readme = _fence(
            "uv sync --extra alpha --group=dev",
            "uv sync --extra bfcl-v4",
            "pip install my-evals[alpha,bfcl_v4]",
            'uv pip install -e ".[alpha]"',
        )
        results = _deps(tmp_path, readme + "Or `pip install my_evals[alpha]`.\n")
        assert [r.status for r in results] == ["pass"]
        assert "7 extra(s)" in results[0].message

    def test_fails_at_each_missing_name(self, tmp_path):
        readme = _fence(
            "uv sync --extra fortress",
            "uv run \\",
            "  --group alpha inspect eval my_evals/alpha",
            "pip install my_evals[dev]",
        )
        results = _deps(tmp_path, readme)
        assert [(r.status, r.line) for r in results] == [("fail", 4), ("fail", 6), ("fail", 7)]
        assert "--extra fortress" in results[0].message
        assert "pyproject.toml defines no extra 'fortress'" in results[0].message
        assert "drop it" in (results[0].hint or "")
        assert "use --extra alpha" in (results[1].hint or "")
        assert "my_evals[dev]" in results[2].message
        assert "uv sync --group dev" in (results[2].hint or "")

    def test_ignores_other_projects_and_placeholders(self, tmp_path):
        readme = _fence(
            "pip install openai[realtime]",
            "uv --project other sync --extra nope",
            "uv sync --directory=other --group nope",
            "uv sync --package other --extra nope",
            "uv sync --extra <eval_name>",
            "echo my_evals[nope]",
            "git add my_evals[nope].txt",
            "pip show my_evals[nope]",
        )
        assert [r.status for r in _deps(tmp_path, readme)] == ["skip"]

    def test_reads_every_pip_spelling_and_uv_add(self, tmp_path):
        readme = _fence(
            "pip3 install my_evals[a]",
            "python -m pip install --upgrade my_evals[b]",
            "uv pip install my_evals[c]",
            "uv add my_evals[d]",
        )
        assert [(r.status, r.line) for r in _deps(tmp_path, readme)] == [
            ("fail", 4),
            ("fail", 5),
            ("fail", 6),
            ("fail", 7),
        ]

    def test_stops_reading_a_block_at_cd(self, tmp_path):
        readme = (
            _fence(
                "git clone https://github.com/x/harness && cd harness && pip install -e '.[all]'"
            )
            + _fence("uv sync --extra alpha", "cd ../harness", "uv sync --extra gpu")
            + _fence("uv sync --extra nope")
        )
        results = _deps(tmp_path, readme)
        assert [(r.status, r.line) for r in results] == [("fail", 16)]

    def test_skips_a_pyproject_that_is_not_toml(self, tmp_path):
        results = _deps(tmp_path, _fence("uv sync --extra alpha"), "[project\n")
        assert [r.status for r in results] == ["skip"]
        assert "Could not parse pyproject.toml" in results[0].message

    def test_skips_a_readme_that_is_not_utf8(self, tmp_path):
        eval_dir = tmp_path / "alpha"
        write(eval_dir / "__init__.py", "")
        write(tmp_path / "pyproject.toml", PYPROJECT)
        (eval_dir / "README.md").write_bytes(b"# Latin-1 \xe9\n")
        results = list(readme_dependency_groups(context_for(eval_dir)))
        assert [r.status for r in results] == ["skip"]
        assert "Could not read README.md" in results[0].message

    def test_isolated_package_governs_its_evaluation(self, tmp_path):
        config = make_monorepo(tmp_path)
        write(
            tmp_path / "packages/alpha/pyproject.toml",
            '[project]\nname = "inspect-evals-alpha"\n\n[dependency-groups]\ndev = ["pytest"]\n',
        )
        write(
            config.package_dir(tmp_path, "alpha") / "README.md",
            _fence(
                "uv sync --group dev",
                "uv run --group alpha inspect eval inspect_evals/alpha",
                "pip install inspect_evals[nope]",
            ),
        )
        report = lint_package(tmp_path, "alpha", config, check="readme_dependency_groups")
        isolated, root = report.diagnostics
        assert (isolated.status, isolated.line) == ("fail", 5)
        assert "packages/alpha/pyproject.toml, which governs this evaluation" in isolated.message
        assert "uv sync` in packages/alpha/" in (isolated.hint or "")
        # The root project's extras are the root's business, not the isolated package's.
        assert (root.status, root.line) == ("fail", 6)
        assert "but pyproject.toml defines no extra 'nope'" in root.message
        assert "packages/alpha" not in root.message + (root.hint or "")

    def test_standalone_repo_reads_the_root_readme_and_pyproject(self, tmp_path):
        config = make_register_repo(tmp_path)
        write(tmp_path / "README.md", _fence("uv sync --extra test"))
        report = lint_package(tmp_path, "alpha", config, check="readme_dependency_groups")
        assert [(d.status, d.line) for d in report.diagnostics] == [("fail", 4)]
