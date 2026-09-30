"""host_code_execution: model-controlled input reaching exec, eval, subprocess and friends on the host."""

from dataclasses import replace
from pathlib import Path

import pytest

from inspect_evals_lint.config import PRESETS, LintConfig
from inspect_evals_lint.diagnostics import Diagnostic, Outcome
from inspect_evals_lint.rules.host_code import host_code_execution
from tests.conftest import context_for, write


def run(tmp_path: Path, code: str, *, config: LintConfig | None = None, name: str = "my_eval"):
    package = tmp_path / name
    write(package / "__init__.py", "")
    write(package / "tools.py", code)
    return list(host_code_execution(context_for(package, config)))


def diagnostics(results) -> list[Diagnostic]:
    return [r for r in results if isinstance(r, Diagnostic)]


def statuses(results) -> list[str]:
    return [r.status for r in results]


class TestSources:
    def test_tool_argument_reaching_eval_fails(self, tmp_path):
        results = run(
            tmp_path,
            """
from inspect_ai.tool import tool

@tool
def calculate():
    async def execute(expression: str) -> str:
        return str(eval(expression, {"__builtins__": None}, {}))
    return execute
""",
        )
        (d,) = diagnostics(results)
        assert d.status == "fail"
        assert d.line == 7
        assert d.key == "tools.py:eval"
        assert "tool argument 'expression'" in d.message
        assert "'tools.py:eval' in allowlists.host_code_execution" in (d.hint or "")

    def test_tool_factory_parameters_are_not_model_controlled(self, tmp_path):
        results = run(
            tmp_path,
            """
@tool
def runner(program: str):
    async def execute(arg: str) -> str:
        return str(eval(program))
    return execute
""",
        )
        assert statuses(results) == ["warn"]

    @pytest.mark.parametrize(
        "expression",
        [
            "state.output.completion",
            "state.output.message.text",
            "state.messages[-1].text",
            "state.output.message.tool_calls[0].arguments['code']",
            "call.arguments['code']",
        ],
    )
    def test_model_output_reaching_exec_fails(self, tmp_path, expression):
        results = run(
            tmp_path,
            f"""
async def solve(state, generate):
    exec({expression})
""",
        )
        assert statuses(results) == ["fail"]
        assert "model output" in diagnostics(results)[0].message

    def test_model_output_annotated_parameter_is_tainted(self, tmp_path):
        results = run(
            tmp_path,
            """
from inspect_ai.model import ModelOutput

def process(output: ModelOutput) -> None:
    exec(output.message.text)
""",
        )
        assert statuses(results) == ["fail"]

    def test_sandbox_read_file_reaching_exec_fails(self, tmp_path):
        results = run(
            tmp_path,
            """
from inspect_ai.util import sandbox

async def score(state, target):
    code = await sandbox().read_file("/home/agent/solution.py")
    exec(code)
""",
        )
        (d,) = diagnostics(results)
        assert d.status == "fail"
        assert "sandbox read_file()" in d.message

    def test_sandbox_exec_stdout_through_a_bound_sandbox_fails(self, tmp_path):
        results = run(
            tmp_path,
            """
import pickle
from inspect_ai.util import sandbox

async def score(state, target):
    sb = sandbox("victim")
    result = await sb.exec(["cat", "/tmp/out"])
    return pickle.loads(result.stdout.encode())
""",
        )
        (d,) = diagnostics(results)
        assert d.status == "fail"
        assert d.key == "tools.py:pickle.loads"

    def test_sandbox_environment_parameter(self, tmp_path):
        results = run(
            tmp_path,
            """
import os
from inspect_ai.util import SandboxEnvironment

async def collect(env: SandboxEnvironment) -> None:
    listing = await env.read_file("run.sh")
    os.system(listing)
""",
        )
        assert statuses(results) == ["fail"]

    def test_untainted_sink_warns_once_without_a_key(self, tmp_path):
        results = run(tmp_path, 'CODE = "x = 1"\nexec(CODE)\n')
        (d,) = diagnostics(results)
        assert d.status == "warn"
        assert d.key is None
        assert "ignore[host_code_execution]" in (d.hint or "")


class TestPropagation:
    def test_fstring_concatenation_and_format(self, tmp_path):
        results = run(
            tmp_path,
            """
async def solve(state, generate):
    body = state.output.completion
    wrapped = f"def f():\\n{body}"
    joined = "import os\\n" + wrapped
    final = "{}\\nf()".format(joined)
    exec(final)
""",
        )
        assert statuses(results) == ["fail"]

    def test_unpacking_loops_with_and_walrus(self, tmp_path):
        results = run(
            tmp_path,
            """
async def solve(state, generate):
    for call in state.output.message.tool_calls:
        _, code = "x", call
        if (snippet := code):
            with open(snippet) as handle:
                exec(handle.read())
""",
        )
        assert statuses(results) == ["fail"]

    def test_container_mutation_taints_the_container(self, tmp_path):
        results = run(
            tmp_path,
            """
async def solve(state, generate):
    parts = []
    parts.append(state.output.completion)
    exec("\\n".join(parts))
""",
        )
        assert statuses(results) == ["fail"]

    def test_attribute_and_subscript_targets(self, tmp_path):
        results = run(
            tmp_path,
            """
class Solver:
    def process(self, state):
        self.code = state.output.completion
        store = {}
        store["code"] = self.code
        eval(store["code"])
""",
        )
        assert statuses(results) == ["fail"]

    def test_taint_is_flow_insensitive_within_a_function(self, tmp_path):
        """A variable is tainted if any assignment to it is, wherever the sink is."""
        results = run(
            tmp_path,
            """
async def solve(state, generate):
    code = "pass"
    exec(code)
    code = state.output.completion
""",
        )
        assert statuses(results) == ["fail"]

    def test_closures_see_the_enclosing_function_taint(self, tmp_path):
        results = run(
            tmp_path,
            """
async def solve(state, generate):
    code = state.output.completion
    def run():
        exec(code)
    run()
""",
        )
        assert statuses(results) == ["fail"]

    def test_one_level_same_file_call_reports_at_the_sink(self, tmp_path):
        """The gdm_self_reasoning shape: the host passes agent-written code to a helper that execs it."""
        results = run(
            tmp_path,
            """
from inspect_ai.util import sandbox


def passes_tests(module_code: str) -> bool:
    namespace = {}
    exec(module_code, namespace)
    return "f" in namespace


async def score(state, target):
    written = await sandbox().read_file("gol.py")
    return passes_tests(written)
""",
        )
        (d,) = diagnostics(results)
        assert (d.status, d.line) == ("fail", 7)
        assert "via passes_tests() from line 13" in d.message

    def test_callee_defined_after_the_caller_keeps_untraced_sites_plain(self, tmp_path):
        results = run(
            tmp_path,
            """
def score(state):
    run_both(state.output.completion)


def run_both(code):
    exec(code)
    exec("print(1)")
""",
        )
        assert [(d.status, d.line, "via" in d.message) for d in diagnostics(results)] == [
            ("fail", 7, True),
            ("warn", 8, False),
        ]

    def test_keyword_arguments_and_methods_via_self(self, tmp_path):
        results = run(
            tmp_path,
            """
class Checker:
    def check(self, *, source: str) -> None:
        eval(source)

    def score(self, state) -> None:
        self.check(source=state.output.completion)
""",
        )
        (d,) = diagnostics(results)
        assert (d.status, d.line) == ("fail", 4)

    def test_unpacked_keyword_arguments_taint_every_parameter(self, tmp_path):
        results = run(
            tmp_path,
            """
def check(code, namespace=None):
    exec(code, namespace)


def score(state):
    options = {"code": state.output.completion}
    check(**options)
""",
        )
        assert statuses(results) == ["fail"]

    def test_same_file_return_value_carries_taint(self, tmp_path):
        results = run(
            tmp_path,
            """
from inspect_ai.util import sandbox


async def read_answer() -> str:
    return await sandbox().read_file("answer.py")


async def score(state, target):
    exec(await read_answer())
""",
        )
        assert statuses(results) == ["fail"]

    def test_calls_are_followed_one_level_only(self, tmp_path):
        results = run(
            tmp_path,
            """
def inner(code):
    exec(code)


def outer(code):
    inner(code)


async def solve(state, generate):
    outer(state.output.completion)
""",
        )
        assert statuses(results) == ["warn"]

    def test_model_input_in_the_exec_namespace_is_not_code(self, tmp_path):
        """The gdm_self_reasoning common_tools shape: constant code, the query passed as data."""
        results = run(
            tmp_path,
            """
@tool
def query_database():
    async def execute(query: str) -> str:
        variables = locals()
        exec(QUERY_CODE + "\\n\\noutput = query_database(query)", variables)
        return str(variables.get("output"))
    return execute
""",
        )
        assert statuses(results) == ["warn"]


class TestCodeSinks:
    @pytest.mark.parametrize("sink", ["exec", "eval", "compile", "__import__"])
    def test_builtin_sinks(self, tmp_path, sink):
        results = run(tmp_path, f"def f(state):\n    {sink}(state.output.completion)\n")
        (d,) = diagnostics(results)
        assert d.status == "fail"
        assert d.key == f"tools.py:{sink}"

    def test_builtins_module_spelling(self, tmp_path):
        results = run(
            tmp_path,
            "import builtins\n\ndef f(state):\n    builtins.exec(state.output.completion)\n",
        )
        assert [d.key for d in diagnostics(results)] == ["tools.py:exec"]

    def test_inspect_ai_eval_is_not_the_builtin(self, tmp_path):
        results = run(
            tmp_path,
            """
from inspect_ai import eval

def run_it(state):
    eval(state.output.completion, model="mockllm/model")
""",
        )
        assert statuses(results) == ["pass"]

    def test_importing_inspect_ai_eval_leaves_the_other_builtins_checked(self, tmp_path):
        results = run(
            tmp_path,
            """
from inspect_ai import eval

def run_it(state):
    exec(state.output.completion)
    compile(state.output.completion, "<model>", "exec")
    __import__(state.output.completion)
""",
        )
        assert [d.key for d in diagnostics(results)] == [
            "tools.py:exec",
            "tools.py:compile",
            "tools.py:__import__",
        ]

    def test_inspect_ai_eval_under_an_alias_leaves_builtin_eval_checked(self, tmp_path):
        results = run(
            tmp_path,
            """
from inspect_ai import eval as inspect_eval

def run_it(state):
    eval(state.output.completion)
""",
        )
        assert [d.key for d in diagnostics(results)] == ["tools.py:eval"]

    def test_sandbox_exec_and_re_compile_are_not_sinks(self, tmp_path):
        results = run(
            tmp_path,
            """
import re
from inspect_ai.util import sandbox

async def solve(state, generate):
    await sandbox().exec(["python", "-c", state.output.completion])
    re.compile(state.output.completion)
""",
        )
        assert statuses(results) == ["pass"]


class TestProcessSinks:
    def test_constant_argv_without_a_shell_is_not_reported(self, tmp_path):
        results = run(
            tmp_path,
            'import subprocess\n\ndef f():\n    subprocess.run(["git", "status"], check=True)\n',
        )
        assert statuses(results) == ["pass"]

    def test_tainted_argument_to_a_constant_program_is_not_reported(self, tmp_path):
        results = run(
            tmp_path,
            'import subprocess\n\ndef f(state):\n    subprocess.run(["grep", state.output.completion, "log.txt"])\n',
        )
        assert statuses(results) == ["pass"]

    def test_tainted_program_fails(self, tmp_path):
        results = run(
            tmp_path,
            "from subprocess import Popen\n\ndef f(state):\n    Popen([state.output.completion, '-v'])\n",
        )
        (d,) = diagnostics(results)
        assert d.status == "fail"
        assert d.key == "tools.py:subprocess.Popen"

    def test_tainted_executable_keyword_fails(self, tmp_path):
        results = run(
            tmp_path,
            "import subprocess\n\ndef f(state):\n    subprocess.run(['x'], executable=state.output.completion)\n",
        )
        assert statuses(results) == ["fail"]

    def test_constant_shell_command_warns(self, tmp_path):
        results = run(
            tmp_path, 'import subprocess\n\ndef f():\n    subprocess.run("ls -la", shell=True)\n'
        )
        assert statuses(results) == ["warn"]

    def test_tainted_shell_command_fails(self, tmp_path):
        results = run(
            tmp_path,
            'import subprocess as sp\n\ndef f(state):\n    sp.check_output(f"echo {state.output.completion}", shell=True)\n',
        )
        (d,) = diagnostics(results)
        assert d.status == "fail"
        assert d.key == "tools.py:subprocess.check_output"

    def test_non_literal_argv_carrying_taint_warns(self, tmp_path):
        """The program could be the model's or a constant; a reviewer decides."""
        results = run(
            tmp_path,
            """
import subprocess

def f(state):
    argv = ["python", state.output.completion]
    subprocess.run(argv)
""",
        )
        assert statuses(results) == ["warn"]

    @pytest.mark.parametrize(
        ("call", "key"),
        [
            ("os.system(state.output.completion)", "os.system"),
            ("os.popen(state.output.completion)", "os.popen"),
            ("subprocess.getoutput(state.output.completion)", "subprocess.getoutput"),
            (
                "await asyncio.create_subprocess_shell(state.output.completion)",
                "asyncio.create_subprocess_shell",
            ),
            (
                "await asyncio.create_subprocess_exec(state.output.completion, '-c', 'x')",
                "asyncio.create_subprocess_exec",
            ),
        ],
    )
    def test_shell_and_exec_helpers(self, tmp_path, call, key):
        results = run(
            tmp_path,
            f"import asyncio\nimport os\nimport subprocess\n\nasync def f(state):\n    {call}\n",
        )
        (d,) = diagnostics(results)
        assert (d.status, d.key) == ("fail", f"tools.py:{key}")

    def test_os_system_with_a_constant_warns(self, tmp_path):
        results = run(tmp_path, 'import os\n\ndef f():\n    os.system("make")\n')
        assert statuses(results) == ["warn"]


class TestDeserialisationAndImports:
    def test_yaml_load_without_a_safe_loader(self, tmp_path):
        results = run(
            tmp_path,
            "import yaml\n\ndef f(state):\n    yaml.load(state.output.completion, Loader=yaml.Loader)\n",
        )
        assert [d.key for d in diagnostics(results)] == ["tools.py:yaml.load"]

    @pytest.mark.parametrize(
        "loader", ["Loader=yaml.SafeLoader", "yaml.CSafeLoader", "Loader=SafeLoader"]
    )
    def test_yaml_load_with_a_safe_loader_passes(self, tmp_path, loader):
        results = run(
            tmp_path,
            f"import yaml\nfrom yaml import SafeLoader\n\ndef f(state):\n    yaml.load(state.output.completion, {loader})\n",
        )
        assert statuses(results) == ["pass"]

    def test_yaml_loader_subclassing_a_safe_loader_in_the_same_file_passes(self, tmp_path):
        results = run(
            tmp_path,
            """
import yaml

class ImportLoader(yaml.SafeLoader):
    pass

class IncludeLoader(ImportLoader):
    pass

def f(stream):
    yaml.load(stream, IncludeLoader)
""",
        )
        assert statuses(results) == ["pass"]

    def test_yaml_loader_subclassing_the_full_loader_warns(self, tmp_path):
        results = run(
            tmp_path,
            "import yaml\n\nclass Custom(yaml.Loader):\n    pass\n\ndef f(stream):\n    yaml.load(stream, Custom)\n",
        )
        assert statuses(results) == ["warn"]

    def test_yaml_unsafe_load(self, tmp_path):
        results = run(tmp_path, "import yaml\n\ndef f(text):\n    yaml.unsafe_load(text)\n")
        assert statuses(results) == ["warn"]

    def test_import_module_only_when_tainted(self, tmp_path):
        results = run(
            tmp_path,
            """
import importlib

def f(state, name):
    importlib.import_module(f"pkg.{name}")
    importlib.import_module(state.output.completion)
""",
        )
        (d,) = diagnostics(results)
        assert (d.status, d.key, d.line) == ("fail", "tools.py:importlib.import_module", 6)


class TestHostCode:
    def test_excluded_files_are_sandbox_code(self, tmp_path):
        package = tmp_path / "my_eval"
        write(package / "__init__.py", "")
        write(package / "challenges" / "app.py", "import os\nos.system(input())\n")
        config = replace(PRESETS["multi-eval"], exclude=("my_eval/challenges/**",))
        assert statuses(host_code_execution(context_for(package, config))) == ["pass"]
        assert statuses(host_code_execution(context_for(package))) == ["warn"]

    def test_key_is_the_path_within_the_package(self, tmp_path):
        package = tmp_path / "my_eval"
        write(package / "__init__.py", "")
        write(package / "common" / "tools.py", "def f(state):\n    eval(state.output.completion)\n")
        (d,) = diagnostics(host_code_execution(context_for(package)))
        assert d.key == "common/tools.py:eval"

    def test_unparsable_file_is_reported(self, tmp_path):
        results = run(tmp_path, "def broken(:\n")
        assert statuses(results) == ["fail"]
        assert "Could not parse" in results[0].message

    def test_pass_outcome_when_nothing_is_found(self, tmp_path):
        results = run(tmp_path, "def f():\n    return 1\n")
        assert len(results) == 1
        assert isinstance(results[0], Outcome)
        assert results[0].status == "pass"

    def test_sites_are_reported_in_source_order(self, tmp_path):
        results = run(
            tmp_path,
            """
def later(state):
    eval(state.output.completion)

def earlier():
    exec("x")
""",
        )
        assert [d.line for d in diagnostics(results)] == [3, 6]
