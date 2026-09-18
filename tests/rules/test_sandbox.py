"""Sandbox rules: image pinning and the GPU sandbox check."""

from inspect_evals_lint.rules.sandbox import (
    gpu_sandbox_check,
    sandbox_image_pinning,
)
from tests.conftest import context_for


class TestSandboxImagePinning:
    """Tests for the sandbox_image_pinning rule."""

    def run_check(self, tmp_path, compose_content, name="my_eval"):
        eval_path = tmp_path / name
        eval_path.mkdir()
        (eval_path / "compose.yaml").write_text(compose_content)
        return list(sandbox_image_pinning(context_for(eval_path)))

    def test_untagged_registry_image_fails(self, tmp_path):
        results = self.run_check(
            tmp_path,
            "services:\n  default:\n    image: aisiuk/inspect-tool-support\n",
        )
        assert [r.status for r in results] == ["fail"]
        assert "aisiuk/inspect-tool-support" in results[0].message

    def test_latest_tag_fails(self, tmp_path):
        results = self.run_check(
            tmp_path,
            "services:\n  default:\n    image: ghcr.io/example/thing:latest\n",
        )
        assert [r.status for r in results] == ["fail"]

    def test_version_tag_passes(self, tmp_path):
        results = self.run_check(
            tmp_path,
            "services:\n  default:\n    image: collabora/code:24.04.9.2.1\n",
        )
        assert [r.status for r in results] == ["pass"]

    def test_build_date_tag_passes(self, tmp_path):
        results = self.run_check(
            tmp_path,
            "services:\n  default:\n    image: ghcr.io/generality-labs/inspect-eval-ds10000:2026-08-03\n",
        )
        assert [r.status for r in results] == ["pass"]

    def test_tag_and_digest_passes(self, tmp_path):
        results = self.run_check(
            tmp_path,
            "services:\n  default:\n    image: aisiuk/evals-cybench-agent-sandbox:1.0.0@sha256:"
            + "a" * 64
            + "\n",
        )
        assert [r.status for r in results] == ["pass"]

    def test_digest_only_passes(self, tmp_path):
        results = self.run_check(
            tmp_path,
            "services:\n  default:\n    image: aisiuk/thing@sha256:" + "b" * 64 + "\n",
        )
        assert [r.status for r in results] == ["pass"]

    def test_registry_port_untagged_fails(self, tmp_path):
        results = self.run_check(
            tmp_path,
            "services:\n  default:\n    image: localhost:5000/my-image\n",
        )
        assert [r.status for r in results] == ["fail"]

    def test_built_service_is_ignored(self, tmp_path):
        results = self.run_check(
            tmp_path,
            "services:\n  default:\n    build: .\n    image: my-local-image\n",
        )
        assert [r.status for r in results] == ["pass"]

    def test_env_interpolated_image_is_ignored(self, tmp_path):
        results = self.run_check(
            tmp_path,
            "services:\n  default:\n    image: ${SAMPLE_METADATA_IMAGE_FIXED}\n",
        )
        assert [r.status for r in results] == ["pass"]

    def test_no_compose_files_skips(self, tmp_path):
        eval_path = tmp_path / "my_eval"
        eval_path.mkdir()
        results = list(sandbox_image_pinning(context_for(eval_path)))
        assert [r.status for r in results] == ["skip"]

    def test_diagnostics_are_keyed_by_image_for_the_allowlist(self, tmp_path):
        results = self.run_check(
            tmp_path,
            "services:\n  default:\n    image: example/untagged\n",
        )
        assert [r.key for r in results] == ["example/untagged"]

    def test_nested_compose_files_are_checked(self, tmp_path):
        eval_path = tmp_path / "my_eval"
        (eval_path / "challenges" / "foo").mkdir(parents=True)
        (eval_path / "challenges" / "foo" / "compose.yml").write_text(
            "services:\n  default:\n    image: example/untagged\n"
        )
        results = list(sandbox_image_pinning(context_for(eval_path)))
        assert [r.status for r in results] == ["fail"]

    def test_invalid_yaml_warns(self, tmp_path):
        results = self.run_check(tmp_path, "services: [unclosed\n")
        assert [r.status for r in results] == ["warn"]


class TestGpuSandboxCheck:
    """Tests for the gpu_sandbox_check rule."""

    GPU_TASKS = "tasks:\n  - name: my_eval\n    dataset_samples: 10\n"

    def run_check(self, tmp_path, eval_yaml, name="my_eval"):
        eval_path = tmp_path / name
        eval_path.mkdir()
        if eval_yaml is not None:
            (eval_path / "eval.yaml").write_text(eval_yaml)
        return list(gpu_sandbox_check(context_for(eval_path)))

    def test_missing_eval_yaml_skips(self, tmp_path):
        results = self.run_check(tmp_path, None)
        assert [r.status for r in results] == ["skip"]

    def test_no_gpu_requirement_skips(self, tmp_path):
        results = self.run_check(
            tmp_path, self.GPU_TASKS + "metadata:\n  requires:\n    internet: true\n"
        )
        assert [r.status for r in results] == ["skip"]

    def test_gpu_false_skips(self, tmp_path):
        results = self.run_check(
            tmp_path, self.GPU_TASKS + "metadata:\n  requires:\n    gpu: false\n"
        )
        assert [r.status for r in results] == ["skip"]

    def test_gpu_eval_without_check_task_fails(self, tmp_path):
        results = self.run_check(
            tmp_path,
            self.GPU_TASKS + "metadata:\n  requires:\n    gpu:\n      count: 1\n",
        )
        assert [r.status for r in results] == ["fail"]
        assert "sandbox check task" in results[0].message
        assert "kind: maintenance" in (results[0].hint or "")

    def test_gpu_eval_with_maintenance_check_task_passes(self, tmp_path):
        results = self.run_check(
            tmp_path,
            self.GPU_TASKS
            + "  - name: my_eval_sandbox_check\n    dataset_samples: 3\n    kind: maintenance\n"
            + "metadata:\n  requires:\n    gpu: true\n",
        )
        assert [r.status for r in results] == ["pass"]
        assert "my_eval_sandbox_check" in results[0].message

    def test_check_task_must_be_declared_maintenance(self, tmp_path):
        results = self.run_check(
            tmp_path,
            self.GPU_TASKS
            + "  - name: my_eval_sandbox_check\n    dataset_samples: 3\n"
            + "metadata:\n  requires:\n    gpu: true\n",
        )
        assert [r.status for r in results] == ["fail"]
        assert "my_eval_sandbox_check" in results[0].message
        assert "kind: maintenance" in results[0].message

    def test_invalid_yaml_warns(self, tmp_path):
        results = self.run_check(tmp_path, "tasks: [\n")
        assert [r.status for r in results] == ["warn"]
