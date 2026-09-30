"""Sandbox rules for images, runtime privileges, and GPU maintenance checks."""

from dataclasses import replace

import pytest
import yaml

from inspect_evals_lint.config import PRESETS
from inspect_evals_lint.rules.sandbox import (
    gpu_sandbox_check,
    sandbox_image_pinning,
    sandbox_privileges,
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

    def test_docker_compose_files_are_checked(self, tmp_path):
        (tmp_path / "docker-compose.yml").write_text(
            "services:\n  default:\n    image: example/untagged\n"
        )
        results = list(sandbox_image_pinning(context_for(tmp_path)))
        assert [r.status for r in results] == ["fail"]

    def test_excluded_directories_are_still_checked(self, tmp_path):
        eval_path = tmp_path / "my_eval"
        (eval_path / "challenges").mkdir(parents=True)
        (eval_path / "challenges" / "compose.yaml").write_text(
            "services:\n  default:\n    image: example/untagged\n"
        )
        config = replace(PRESETS["multi-eval"], exclude=("my_eval/challenges/**",))
        results = list(sandbox_image_pinning(context_for(eval_path, config)))
        assert [r.status for r in results] == ["fail"]

    def test_directory_named_like_a_compose_file_is_ignored(self, tmp_path):
        (tmp_path / "compose.yaml").mkdir()
        results = list(sandbox_image_pinning(context_for(tmp_path)))
        assert [r.status for r in results] == ["skip"]

    def test_non_utf8_file_warns(self, tmp_path):
        (tmp_path / "compose.yaml").write_bytes(b"services:\n  default:\n    image: \xff\n")
        results = list(sandbox_image_pinning(context_for(tmp_path)))
        assert [r.status for r in results] == ["warn"]


class TestSandboxPrivileges:
    def run_check(self, tmp_path, service, *, volumes=None):
        compose = {"services": {"default": service}}
        if volumes is not None:
            compose["volumes"] = volumes
        (tmp_path / "compose.yaml").write_text(yaml.safe_dump(compose))
        return list(sandbox_privileges(context_for(tmp_path)))

    @pytest.mark.parametrize(
        ("field", "value", "key"),
        [
            ("privileged", True, "privileged"),
            ("privileged", "true", "privileged"),
            ("use_api_socket", True, "use_api_socket"),
            ("cap_add", ["SYS_PTRACE"], "cap_add:SYS_PTRACE"),
            ("cap_add", ["ALL"], "cap_add:ALL"),
            ("devices", ["/dev/kvm:/dev/kvm"], "devices:/dev/kvm:/dev/kvm"),
            ("device_cgroup_rules", ["c 1:3 mr"], "device_cgroup_rules:c 1:3 mr"),
            ("network_mode", "host", "network_mode:host"),
            ("pid", "host", "pid:host"),
            ("ipc", "host", "ipc:host"),
            ("userns_mode", "host", "userns_mode:host"),
            ("uts", "host", "uts:host"),
            ("cgroup", "host", "cgroup:host"),
            ("network_mode", "container:outside", "network_mode:container:outside"),
            ("pid", "container:outside", "pid:container:outside"),
            ("ipc", "container:outside", "ipc:container:outside"),
            ("volumes_from", ["container:outside:ro"], "volumes_from:container:outside:ro"),
        ],
    )
    def test_service_privilege_fails_with_allowlist_key(self, tmp_path, field, value, key):
        (result,) = self.run_check(tmp_path, {field: value})
        assert result.status == "fail"
        assert result.key == f"default:{key}"
        assert result.file == tmp_path / "compose.yaml"

    @pytest.mark.parametrize("option", ["seccomp", "apparmor", "label", "systempaths"])
    @pytest.mark.parametrize("separator", [":", "="])
    def test_security_restrictions(self, tmp_path, option, separator):
        value = "disable" if option == "label" else "unconfined"
        setting = f"{option}{separator}{value}"
        (result,) = self.run_check(tmp_path, {"security_opt": [setting]})
        assert result.status == "fail"
        assert result.key == f"default:security_opt:{setting}"
        assert setting in result.message

    @pytest.mark.parametrize("hook", ["pre_start", "post_start", "pre_stop"])
    def test_privileged_lifecycle_hooks(self, tmp_path, hook):
        (result,) = self.run_check(
            tmp_path, {hook: [{"command": "./setup.sh", "privileged": True}]}
        )
        assert result.status == "fail"
        assert result.key == f"default:{hook}.privileged"

    @pytest.mark.parametrize(
        "mount",
        [
            "/host:/container",
            "./data:/data:ro",
            "../data:/data",
            ".:/workspace",
            "..:/workspace",
            r".\data:/data",
            "~/data:/data",
            r"C:\data:/data",
            r"C:\data:C:\container",
            r"\\server\share:/data",
            {"type": "bind", "source": "/host", "target": "/container", "read_only": True},
            {"type": "bind", "source": "${HOST_PATH}", "target": "/container"},
            {
                "type": "npipe",
                "source": "//./pipe/docker_engine",
                "target": "//./pipe/docker_engine",
            },
        ],
    )
    def test_host_mounts(self, tmp_path, mount):
        (result,) = self.run_check(tmp_path, {"volumes": [mount]})
        assert result.status == "fail"
        assert result.key is not None
        assert result.key.startswith("default:volumes:")

    @pytest.mark.parametrize(
        "source", ["/var/run/docker.sock", "/run/docker.sock", "/run/user/1000/docker.sock"]
    )
    def test_docker_socket_is_identified(self, tmp_path, source):
        (result,) = self.run_check(tmp_path, {"volumes": [f"{source}:/socket:ro"]})
        assert result.status == "fail"
        assert "Docker daemon socket" in result.message

    @pytest.mark.parametrize(
        "mount", ["data:/data", {"type": "volume", "source": "data", "target": "/data"}]
    )
    @pytest.mark.parametrize("mode", ["bind", "ro,bind", "rbind"])
    def test_named_volume_backed_by_host_bind(self, tmp_path, mount, mode):
        (result,) = self.run_check(
            tmp_path,
            {"volumes": [mount]},
            volumes={"data": {"driver_opts": {"type": "none", "o": mode, "device": "/host"}}},
        )
        assert result.status == "fail"
        assert result.key == "default:volumes:/host"
        assert "/host" in result.message

    def test_ordinary_settings_and_gpu_reservations_pass(self, tmp_path):
        (result,) = self.run_check(
            tmp_path,
            {
                "privileged": False,
                "use_api_socket": "false",
                "cap_add": [],
                "cap_drop": ["ALL"],
                "devices": [],
                "device_cgroup_rules": [],
                "network_mode": "none",
                "pid": "service:worker",
                "ipc": "shareable",
                "cgroup": "private",
                "user": "root",
                "security_opt": [
                    "no-new-privileges:true",
                    "seccomp=profile.json",
                    "apparmor=my-profile",
                ],
                "volumes": [
                    "/data",
                    "/cache:ro",
                    "/cache:ro,z",
                    "data:/data",
                    {"type": "volume", "source": "data", "target": "/data"},
                ],
                "volumes_from": ["worker:ro"],
                "post_start": [{"command": "true", "privileged": False}],
                "deploy": {
                    "resources": {
                        "reservations": {"devices": [{"driver": "nvidia", "capabilities": ["gpu"]}]}
                    }
                },
            },
            volumes={"data": None, "unused": {"driver_opts": {"o": "bind", "device": "/host"}}},
        )
        assert result.status == "pass"
        assert "1 service(s)" in result.message

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("privileged", "${PRIVILEGED}"),
            ("use_api_socket", "${USE_SOCKET}"),
            ("network_mode", "${NETWORK_MODE:-host}"),
            ("security_opt", ["seccomp=${PROFILE}"]),
            ("volumes", ["${HOST_PATH}:/data"]),
            ("volumes", [{"type": "volume", "source": "${VOLUME}", "target": "/data"}]),
            ("volumes_from", ["${CONTAINER}"]),
            ("post_start", [{"command": "true", "privileged": "${PRIVILEGED}"}]),
        ],
    )
    def test_unresolved_interpolation_warns(self, tmp_path, field, value):
        (result,) = self.run_check(tmp_path, {field: value})
        assert result.status == "warn"
        assert "cannot be checked" in result.message

    def test_one_finding_per_value(self, tmp_path):
        results = self.run_check(
            tmp_path,
            {
                "privileged": True,
                "cap_add": ["SYS_PTRACE", "NET_ADMIN"],
                "volumes": ["/a:/a", "/b:/b"],
                "security_opt": ["seccomp=unconfined", "apparmor:unconfined", "${OPTION}"],
            },
        )
        assert [(r.key, r.status) for r in results] == [
            ("default:privileged", "fail"),
            ("default:cap_add:SYS_PTRACE", "fail"),
            ("default:cap_add:NET_ADMIN", "fail"),
            ("default:security_opt:seccomp=unconfined", "fail"),
            ("default:security_opt:apparmor:unconfined", "fail"),
            ("default:security_opt:${OPTION}", "warn"),
            ("default:volumes:/a", "fail"),
            ("default:volumes:/b", "fail"),
        ]

    def test_yaml_anchors_comments_and_multiple_files(self, tmp_path):
        (tmp_path / "compose.yaml").write_text(
            "x-defaults: &defaults\n  privileged: true\nservices:\n"
            "  default:\n    <<: *defaults\n"
            "  other:\n    <<: *defaults\n    privileged: false\n    # ipc: host\n"
        )
        nested = tmp_path / "nested"
        nested.mkdir()
        other_file = nested / "docker-compose.override.yml"
        other_file.write_text("services:\n  other:\n    cap_add: [SYS_ADMIN]\n")
        results = list(sandbox_privileges(context_for(tmp_path)))
        assert [(r.key, r.file) for r in results] == [
            ("default:privileged", tmp_path / "compose.yaml"),
            ("other:cap_add:SYS_ADMIN", other_file),
        ]

    @pytest.mark.parametrize(
        "content", ["services: [unclosed", "[]", "services: []", "services:\n  default: []\n"]
    )
    def test_unreadable_services_warn_without_passing(self, tmp_path, content):
        (tmp_path / "compose.yml").write_text(content)
        (result,) = list(sandbox_privileges(context_for(tmp_path)))
        assert result.status == "warn"

    def test_non_utf8_file_warns(self, tmp_path):
        (tmp_path / "compose.yaml").write_bytes(b"services:\n  default:\n    image: \xff\n")
        (result,) = list(sandbox_privileges(context_for(tmp_path)))
        assert result.status == "warn"

    @pytest.mark.parametrize("content", ["", "# placeholder\n", "services:\n"])
    def test_empty_compose_file_passes(self, tmp_path, content):
        (tmp_path / "compose.yaml").write_text(content)
        (result,) = list(sandbox_privileges(context_for(tmp_path)))
        assert result.status == "pass"
        assert "0 service(s)" in result.message

    @pytest.mark.parametrize(
        "content",
        [
            "services:\n  default:\n    privileged: !override true\n",
            "services:\n  default:\n    privileged: true\n    ports: !override ['80:80']\n",
            "services:\n  default:\n    privileged: true\n    build: !reset null\n",
            "services:\n  default: !override\n    privileged: true\n",
            "services:\n  default:\n    cap_add: !reset [SYS_ADMIN]\n",
        ],
    )
    def test_compose_merge_tags_are_checked_as_written(self, tmp_path, content):
        (tmp_path / "compose.override.yaml").write_text(content)
        (result,) = list(sandbox_privileges(context_for(tmp_path)))
        assert result.status == "fail"

    def test_excluded_directories_are_still_checked(self, tmp_path):
        eval_path = tmp_path / "my_eval"
        (eval_path / "challenges").mkdir(parents=True)
        (eval_path / "challenges" / "compose.yaml").write_text(
            "services:\n  default:\n    privileged: true\n"
        )
        config = replace(PRESETS["multi-eval"], exclude=("my_eval/challenges/**",))
        (result,) = list(sandbox_privileges(context_for(eval_path, config)))
        assert result.status == "fail"

    def test_no_compose_files_skips(self, tmp_path):
        (result,) = list(sandbox_privileges(context_for(tmp_path)))
        assert result.status == "skip"


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
