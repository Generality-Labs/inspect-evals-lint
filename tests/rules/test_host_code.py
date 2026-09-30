"""host_code_execution: model-controlled input reaching exec, eval, subprocess and friends on the host."""

from dataclasses import replace
from pathlib import Path

import pytest

from inspect_evals_lint.config import PRESETS, LintConfig
from inspect_evals_lint.diagnostics import Diagnostic, Outcome
from inspect_evals_lint.registry import get_rule
from inspect_evals_lint.rules.host_code import host_code_execution
from inspect_evals_lint.suppressions import apply_suppressions, load_suppressions
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
            "state.output.choices[0].message.text",
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

    @pytest.mark.parametrize(
        ("loader", "status"),
        [
            ('getattr(yaml, "CSafeLoader", yaml.SafeLoader)', "pass"),
            ('getattr(yaml, "CSafeLoader")', "pass"),
            ('getattr(yaml, "CLoader", yaml.SafeLoader)', "warn"),
            ('getattr(yaml, "CSafeLoader", yaml.Loader)', "warn"),
            ("getattr(yaml, name, yaml.SafeLoader)", "warn"),
        ],
    )
    def test_yaml_loader_chosen_with_getattr(self, tmp_path, loader, status):
        results = run(
            tmp_path,
            f"import yaml\n\ndef f(stream, name):\n    yaml.load(stream, Loader={loader})\n",
        )
        assert statuses(results) == [status]

    @pytest.mark.parametrize(
        ("base", "loader", "status"),
        [
            ("yaml.UnsafeLoader", "SafeLoader", "warn"),
            ("yaml.SafeLoader", "SafeLoader", "pass"),
            ("yaml.UnsafeLoader", "yaml.SafeLoader", "pass"),
        ],
    )
    def test_a_same_file_class_is_safe_only_through_its_bases(self, tmp_path, base, loader, status):
        """A local class named after a safe loader is judged by what it subclasses, not its name."""
        results = run(
            tmp_path,
            f"import yaml\n\nclass SafeLoader({base}):\n    pass\n\ndef f(stream):\n    yaml.load(stream, {loader})\n",
        )
        assert statuses(results) == [status]

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


class TestBuiltinRebinding:
    """Only an import that is in force where the call runs rebinds a builtin name."""

    def test_main_guarded_inspect_ai_eval_leaves_functions_checked(self, tmp_path):
        results = run(
            tmp_path,
            """
@tool
def calculate():
    async def execute(expression: str) -> str:
        return str(eval(expression))
    return execute


if __name__ == "__main__":
    from inspect_ai import eval
    eval(calculate())
""",
        )
        assert [(d.status, d.line) for d in diagnostics(results)] == [("fail", 5)]

    def test_function_local_inspect_ai_eval_applies_only_in_that_function(self, tmp_path):
        results = run(
            tmp_path,
            """
def main():
    from inspect_ai import eval
    eval("task.py", model="mockllm/model")


def score(state):
    eval(state.output.completion)
""",
        )
        assert [(d.status, d.line) for d in diagnostics(results)] == [("fail", 8)]

    def test_module_level_import_in_a_try_block_rebinds(self, tmp_path):
        results = run(
            tmp_path,
            """
try:
    from inspect_ai import eval
except ImportError:
    pass


def main():
    eval("task.py")
""",
        )
        assert statuses(results) == ["pass"]


class TestScopeCoverage:
    def test_staticmethod_called_through_self(self, tmp_path):
        results = run(
            tmp_path,
            """
class Runner:
    @staticmethod
    def run_code(code):
        exec(code)

    def score(self, state):
        self.run_code(state.output.completion)
""",
        )
        assert [(d.status, d.line) for d in diagnostics(results)] == [("fail", 5)]

    def test_class_body_statements_are_host_code(self, tmp_path):
        results = run(
            tmp_path,
            """
import pickle

class Config:
    DATA = pickle.load(open("x.pkl", "rb"))
""",
        )
        assert statuses(results) == ["warn"]

    def test_methods_under_a_conditional_in_a_class_body(self, tmp_path):
        results = run(
            tmp_path,
            """
import os
import sys

class Tools:
    if sys.platform == "linux":
        def listing(self):
            os.system("ls")
""",
        )
        assert statuses(results) == ["warn"]

    def test_decorators_defaults_and_class_headers(self, tmp_path):
        results = run(
            tmp_path,
            """
import pickle

def register(value):
    return lambda fn: fn

@register(eval("1"))
def f(x=pickle.loads(b"")):
    return x

class K(base=exec("pass")):
    pass
""",
        )
        assert [d.line for d in diagnostics(results)] == [7, 8, 11]

    def test_nested_functions_see_the_enclosing_sandbox(self, tmp_path):
        results = run(
            tmp_path,
            """
async def score(state, target):
    sb = sandbox()
    async def go():
        exec(await sb.read_file("x.py"))
    await go()
""",
        )
        assert statuses(results) == ["fail"]

    @pytest.mark.parametrize(
        "binding",
        [
            "sb: SandboxEnvironment = sandbox()",
            "sb = get_sandbox()",
            "self.sb = sandbox('victim')",
        ],
    )
    def test_sandbox_bindings(self, tmp_path, binding):
        target = "self.sb" if binding.startswith("self.") else "sb"
        results = run(
            tmp_path,
            f"""
from inspect_ai.util import SandboxEnvironment, sandbox
from inspect_ai.util import sandbox as get_sandbox

class Scorer:
    async def score(self, state):
        {binding}
        exec(await {target}.read_file("x.py"))
""",
        )
        assert statuses(results) == ["fail"]

    def test_starred_and_vararg_arguments_seed_the_callee(self, tmp_path):
        results = run(
            tmp_path,
            """
def run_all(*snippets):
    exec(snippets[0])


def check(code, namespace):
    exec(code, namespace)


def score(state):
    run_all("x", state.output.completion)
    check(*[state.output.completion, {}])
""",
        )
        assert statuses(results) == ["fail", "fail"]


class TestMoreSources:
    @pytest.mark.parametrize(
        "expression",
        [
            "(await get_model().generate(prompt)).message.text",
            "response.message.text",
            "response.choices[0].message.text",
        ],
    )
    def test_generate_results_are_model_output(self, tmp_path, expression):
        results = run(
            tmp_path,
            f"""
from inspect_ai.model import get_model

async def solve(state, prompt):
    response = await get_model().generate(prompt)
    exec({expression})
""",
        )
        (d,) = diagnostics(results)
        assert d.status == "fail"
        assert "model output" in d.message

    def test_solver_generate_function_does_not_taint_the_state(self, tmp_path):
        results = run(
            tmp_path,
            """
async def solve(state, generate):
    state = await generate(state)
    exec(state.metadata["setup"])
""",
        )
        assert statuses(results) == ["warn"]

    @pytest.mark.parametrize(
        "annotation", ["ModelOutput | None", "Optional[ModelOutput]", '"ModelOutput | None"']
    )
    def test_optional_model_output_annotation(self, tmp_path, annotation):
        results = run(
            tmp_path,
            f"def process(output: {annotation}) -> None:\n    exec(output.message.text)\n",
        )
        assert statuses(results) == ["fail"]


class TestMoreSinks:
    def test_names_no_import_binds_are_not_modules(self, tmp_path):
        results = run(
            tmp_path,
            """
from ruamel.yaml import YAML

def f(stream, os):
    yaml = YAML(typ="safe")
    yaml.load(stream)
    os.system("ls")
""",
        )
        assert statuses(results) == ["pass"]

    @pytest.mark.parametrize(
        ("call", "key", "status"),
        [
            (
                "subprocess.getstatusoutput(state.output.completion)",
                "subprocess.getstatusoutput",
                "fail",
            ),
            ("subprocess.call([state.output.completion])", "subprocess.call", "fail"),
            ("subprocess.check_call([state.output.completion])", "subprocess.check_call", "fail"),
            ("subprocess.run('ls', shell=use_shell)", None, "warn"),
            ("pickle.load(file=handle)", None, "warn"),
            ("yaml.full_load(state.output.completion)", "yaml.full_load", "fail"),
            ("yaml.unsafe_load_all(handle)", None, "warn"),
            ("yaml.load_all(handle, Loader=yaml.BaseLoader)", None, "pass"),
            ("torch.load(state.output.completion)", "torch.load", "fail"),
            ("torch.load(handle, weights_only=False)", None, "warn"),
            ("torch.load(handle, weights_only=True)", None, "pass"),
        ],
    )
    def test_more_sinks(self, tmp_path, call, key, status):
        results = run(
            tmp_path,
            f"import pickle\nimport subprocess\nimport torch\nimport yaml\n\ndef f(state, handle, use_shell):\n    {call}\n",
        )
        assert statuses(results) == [status]
        if key is not None:
            assert diagnostics(results)[0].key == f"tools.py:{key}"

    @pytest.mark.parametrize(
        ("call", "status"),
        [
            ('await subprocess(f"ls {state.output.completion}")', "fail"),
            ('await subprocess("ls " + state.output.completion)', "fail"),
            ('await subprocess(f"ls " + "-l " + state.output.completion)', "fail"),
            ('await subprocess("ls %s" % state.output.completion)', "fail"),
            ('await subprocess("ls {}".format(state.output.completion))', "fail"),
            ('await subprocess("make build")', "warn"),
            ('await subprocess(["git", state.output.completion])', "pass"),
            ('await subprocess([state.output.completion, "-v"])', "fail"),
            ('await subprocess((state.output.completion, "-v"))', "fail"),
        ],
    )
    def test_inspect_ai_subprocess_runs_strings_through_a_shell(self, tmp_path, call, status):
        results = run(
            tmp_path,
            f"from inspect_ai.util import subprocess\n\nasync def f(state):\n    {call}\n",
        )
        assert statuses(results) == [status]
        if status == "fail":
            assert diagnostics(results)[0].key == "tools.py:inspect_ai.util.subprocess"

    @pytest.mark.parametrize(
        ("setup", "status"),
        [
            ('cmd = ["python", state.output.completion]', "warn"),
            ("cmd = state.output.completion", "warn"),
            ('cmd = ["make", "build"]', "pass"),
        ],
    )
    def test_inspect_ai_subprocess_with_an_opaque_payload_is_judged_as_an_argv(
        self, tmp_path, setup, status
    ):
        """A variable may hold a list as easily as a string, so it gets subprocess.run(cmd)'s treatment."""
        results = run(
            tmp_path,
            f"from inspect_ai.util import subprocess\n\nasync def f(state):\n    {setup}\n    await subprocess(cmd)\n",
        )
        assert statuses(results) == [status]
        if status == "warn":
            assert "program chosen at runtime" in diagnostics(results)[0].message

    @pytest.mark.parametrize(
        ("argv", "status"),
        [
            ("[state.output.completion]", "fail"),
            ('(state.output.completion, "x")', "fail"),
            ('["ls", state.output.completion]', "warn"),
            ("[]", "warn"),
        ],
    )
    def test_shell_with_a_list_runs_only_its_first_element(self, tmp_path, argv, status):
        """With ``shell=True`` the later elements are the shell's own arguments, not the command."""
        results = run(
            tmp_path,
            f"import subprocess\n\ndef f(state):\n    subprocess.run({argv}, shell=True)\n",
        )
        assert statuses(results) == [status]


class TestSuppression:
    def test_ignore_comment_on_any_line_of_the_call_suppresses_a_warning(self, tmp_path):
        package = tmp_path / "my_eval"
        write(package / "__init__.py", "")
        write(
            package / "tools.py",
            'import os\n\ndef f():\n    os.system(\n        "make"  # inspect-evals-lint: ignore[host_code_execution]\n    )\n',
        )
        ctx = context_for(package)
        (d,) = diagnostics(host_code_execution(ctx))
        d.rule = get_rule("host_code_execution")
        apply_suppressions([d], load_suppressions(ctx), ctx.config, tmp_path)
        assert d.status == "suppressed"


class TestPerScopeNames:
    """A name resolves to a module only through an import in force in the scope that uses it."""

    def test_function_local_inspect_ai_subprocess_leaves_stdlib_subprocess_checked(self, tmp_path):
        results = run(
            tmp_path,
            """
import subprocess

def sandboxed(cmd):
    from inspect_ai.util import subprocess
    return subprocess(["ls"])

def score(state):
    subprocess.run(state.output.completion, shell=True)
""",
        )
        assert [(d.status, d.key) for d in diagnostics(results)] == [
            ("fail", "tools.py:subprocess.run")
        ]

    def test_function_local_stdlib_subprocess_leaves_inspect_ai_subprocess_checked(self, tmp_path):
        results = run(
            tmp_path,
            """
from inspect_ai.util import subprocess

def local():
    import subprocess
    subprocess.run(["ls"])

async def score(state):
    await subprocess(f"echo {state.output.completion}")
""",
        )
        assert [(d.status, d.key) for d in diagnostics(results)] == [
            ("fail", "tools.py:inspect_ai.util.subprocess")
        ]

    def test_local_assignment_shadows_an_imported_module(self, tmp_path):
        results = run(
            tmp_path,
            """
import yaml
from ruamel.yaml import YAML

def f(stream):
    yaml = YAML(typ="safe")
    return yaml.load(stream)
""",
        )
        assert statuses(results) == ["pass"]

    def test_a_parameter_shadows_an_imported_module(self, tmp_path):
        results = run(
            tmp_path,
            """
import os

def f(state, os):
    os.system(state.output.completion)
""",
        )
        assert statuses(results) == ["pass"]

    def test_the_module_is_still_seen_where_nothing_shadows_it(self, tmp_path):
        results = run(
            tmp_path,
            """
import os

def g(os):
    os.system("ls")

def f(state):
    os.system(state.output.completion)
""",
        )
        assert [(d.status, d.line) for d in diagnostics(results)] == [("fail", 8)]


class TestModuleLevelBlocks:
    """A rebinding in a module-level block applies to that block and what it defines, nothing else."""

    GUARD = """
if __name__ == "__main__":
    from inspect_ai import eval
    eval("task.py", model="mockllm/model")
"""

    def test_tainted_module_level_eval_outside_the_guard_fails(self, tmp_path):
        results = run(
            tmp_path,
            "from inspect_ai.util import sandbox\n\n"
            'CODE = sandbox().read_file("x.py")\neval(CODE)\n' + self.GUARD,
        )
        assert [(d.status, d.line) for d in diagnostics(results)] == [("fail", 4)]

    def test_untainted_module_level_eval_outside_the_guard_warns(self, tmp_path):
        results = run(tmp_path, 'eval("1 + 1")\n' + self.GUARD)
        assert [(d.status, d.line) for d in diagnostics(results)] == [("warn", 1)]

    def test_function_defined_inside_the_guard_gets_the_rebinding(self, tmp_path):
        results = run(
            tmp_path,
            """
if __name__ == "__main__":
    from inspect_ai import eval

    def main(state):
        eval(state.output.completion)

    main(None)
""",
        )
        assert statuses(results) == ["pass"]


class TestCalleeContext:
    """A followed call or traced return value is analysed in the scope where the callee was defined."""

    def test_returned_read_through_an_enclosing_sandbox_binding(self, tmp_path):
        results = run(
            tmp_path,
            """
from inspect_ai.util import sandbox

async def score(state, target):
    sb = sandbox()
    async def read():
        return await sb.read_file("a")
    exec(await read())
""",
        )
        (d,) = diagnostics(results)
        assert (d.status, d.line) == ("fail", 8)
        assert "sandbox read_file(), returned by read()" in d.message

    def test_returned_closure_over_a_tool_argument(self, tmp_path):
        results = run(
            tmp_path,
            """
@tool
def run_code():
    async def execute(x: str) -> str:
        def get():
            return x
        exec(get())
        return ""
    return execute
""",
        )
        assert [(d.status, d.line) for d in diagnostics(results)] == [("fail", 7)]

    def test_followed_helper_keeps_its_enclosing_rebinding(self, tmp_path):
        results = run(
            tmp_path,
            """
@tool
def run_task():
    from inspect_ai import eval

    def helper(task):
        eval(task, model="mockllm/model")

    async def execute(task: str) -> str:
        helper(task)
        return ""

    return execute
""",
        )
        assert statuses(results) == ["pass"]


class TestPerformance:
    def test_many_calls_to_one_tainted_helper_are_analysed_once(self, tmp_path):
        import time

        body = "\n".join(f"    v{i} = code + str({i})" for i in range(200))
        calls = "\n".join("    helper(state.output.completion)" for _ in range(300))
        code = f"def helper(code):\n{body}\n    exec(v199)\n\n\ndef score(state):\n{calls}\n"
        start = time.perf_counter()
        results = run(tmp_path, code)
        elapsed = time.perf_counter() - start
        (d,) = diagnostics(results)
        assert d.status == "fail"
        assert "via helper() from line 206" in d.message
        assert elapsed < 0.5


class TestMoreFlows:
    @pytest.mark.parametrize(
        ("pattern", "use"),
        [
            ("code", "code"),
            ("str() as code", "code"),
            ("[first, *others]", "others[0]"),
            ('{"kind": "python", **rest}', 'rest["code"]'),
            ('{"code": str(code)}', "code"),
        ],
    )
    def test_match_captures_take_the_subject(self, tmp_path, pattern, use):
        results = run(
            tmp_path,
            f"""
async def solve(state, generate):
    match state.output.completion:
        case {pattern}:
            exec({use})
""",
        )
        assert statuses(results) == ["fail"]

    def test_lambda_defaults_taint_their_parameters(self, tmp_path):
        results = run(
            tmp_path,
            """
async def solve(state, generate):
    run = lambda code=state.output.completion: exec(code)
    run()
""",
        )
        assert statuses(results) == ["fail"]

    def test_function_defaults_taint_their_parameters(self, tmp_path):
        results = run(
            tmp_path,
            """
async def solve(state, generate):
    def run(namespace, code=state.output.completion, *, mode=None):
        exec(code, namespace)
    run({})
""",
        )
        assert statuses(results) == ["fail"]


_EXEC_SINKS = ["execv", "execve", "execvp", "execvpe", "posix_spawn", "posix_spawnp"]
_EXECL_SINKS = ["execl", "execle", "execlp", "execlpe"]
_SPAWN_SINKS = [
    "spawnv",
    "spawnve",
    "spawnvp",
    "spawnvpe",
    "spawnl",
    "spawnle",
    "spawnlp",
    "spawnlpe",
]


class TestProgramSinks:
    @pytest.mark.parametrize("name", [*_EXEC_SINKS, *_EXECL_SINKS])
    def test_os_exec_program_is_the_first_argument(self, tmp_path, name):
        rest = '["x"], {}' if name in _EXEC_SINKS else '"x", "-v"'
        tainted = run(
            tmp_path,
            f"import os\n\ndef f(state):\n    os.{name}(state.output.completion, {rest})\n",
        )
        (d,) = diagnostics(tainted)
        assert (d.status, d.key) == ("fail", f"tools.py:os.{name}")
        assert "runs a model-controlled program" in d.message
        constant = run(
            tmp_path,
            f'import os\n\ndef f(state):\n    os.{name}("/bin/ls", state.output.completion)\n',
        )
        assert statuses(constant) == ["pass"]

    @pytest.mark.parametrize("name", _SPAWN_SINKS)
    def test_os_spawn_program_follows_the_mode(self, tmp_path, name):
        tainted = run(
            tmp_path,
            f'import os\n\ndef f(state):\n    os.{name}(os.P_WAIT, state.output.completion, "x")\n',
        )
        (d,) = diagnostics(tainted)
        assert (d.status, d.key) == ("fail", f"tools.py:os.{name}")
        constant = run(
            tmp_path,
            f'import os\n\ndef f(state):\n    os.{name}(os.P_WAIT, "/bin/ls", state.output.completion)\n',
        )
        assert statuses(constant) == ["pass"]

    def test_os_exec_with_an_unpacked_argv_warns_when_tainted(self, tmp_path):
        results = run(
            tmp_path,
            "import os\n\ndef f(state):\n    argv = [state.output.completion]\n    os.execv(*argv)\n",
        )
        assert statuses(results) == ["warn"]

    @pytest.mark.parametrize(
        ("call", "status"),
        [
            ("pty.spawn([state.output.completion, '-i'])", "fail"),
            ("pty.spawn(f'{state.output.completion}')", "fail"),
            ("pty.spawn(['bash', state.output.completion])", "pass"),
            ("pty.spawn('bash')", "pass"),
            ("pty.spawn(state.output.completion)", "warn"),
        ],
    )
    def test_pty_spawn_program_is_a_string_or_the_first_element(self, tmp_path, call, status):
        results = run(tmp_path, f"import pty\n\ndef f(state):\n    {call}\n")
        assert statuses(results) == [status]
        if status == "fail":
            assert diagnostics(results)[0].key == "tools.py:pty.spawn"


class TestCodeAndDataSinks:
    @pytest.mark.parametrize(
        ("module", "call"),
        [
            ("runpy", "runpy.run_path"),
            ("runpy", "runpy.run_module"),
            ("marshal", "marshal.loads"),
            ("marshal", "marshal.load"),
            ("dill", "dill.load"),
            ("dill", "dill.loads"),
            ("cloudpickle", "cloudpickle.load"),
            ("cloudpickle", "cloudpickle.loads"),
            ("joblib", "joblib.load"),
            ("pandas", "pandas.read_pickle"),
        ],
    )
    def test_code_and_data_sinks(self, tmp_path, module, call):
        tainted = run(
            tmp_path, f"import {module}\n\ndef f(state):\n    {call}(state.output.completion)\n"
        )
        (d,) = diagnostics(tainted)
        assert (d.status, d.key) == ("fail", f"tools.py:{call}")
        constant = run(tmp_path, f'import {module}\n\ndef f():\n    {call}("local.bin")\n')
        assert statuses(constant) == ["warn"]

    def test_pandas_under_its_usual_alias(self, tmp_path):
        results = run(
            tmp_path, 'import pandas as pd\n\ndef f():\n    pd.read_pickle("scores.pkl")\n'
        )
        assert [d.message.split("()")[0] for d in diagnostics(results)] == ["pandas.read_pickle"]

    @pytest.mark.parametrize(
        ("call", "status"),
        [
            ('np.load("x.npy")', "pass"),
            ('np.load("x.npy", allow_pickle=False)', "pass"),
            ('np.load("x.npy", allow_pickle=True)', "warn"),
            ("np.load(state.output.completion, allow_pickle=flag)", "fail"),
        ],
    )
    def test_numpy_load_only_with_allow_pickle(self, tmp_path, call, status):
        results = run(tmp_path, f"import numpy as np\n\ndef f(state, flag):\n    {call}\n")
        assert statuses(results) == [status]
        if status == "fail":
            assert diagnostics(results)[0].key == "tools.py:numpy.load"
