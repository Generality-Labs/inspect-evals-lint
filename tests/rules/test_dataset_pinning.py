"""Dataset pinning: dataset loads and raw asset URLs must name an immutable commit."""

from inspect_evals_lint.rules.dataset_pinning import dataset_pinning
from tests.conftest import context_for

SHA = "2bb88cae8e6b4b1c0a8b2d1e5b3b9a0e4b1c2d3e"
HF_IMPORT = "from inspect_ai.dataset import hf_dataset\n"


def _run(tmp_path, source: str):
    eval_dir = tmp_path / "alpha"
    eval_dir.mkdir()
    (eval_dir / "data.py").write_text(source, encoding="utf-8")
    return list(dataset_pinning(context_for(eval_dir)))


class TestDatasetPinning:
    def test_missing_revision_fails(self, tmp_path):
        results = _run(
            tmp_path,
            HF_IMPORT + 'ds = hf_dataset(path="allenai/ai2_arc", split="test")\n',
        )
        assert [r.status for r in results] == ["fail"]
        assert "without revision=" in results[0].message
        assert "allenai/ai2_arc" in results[0].message
        assert results[0].key == "allenai/ai2_arc"

    def test_none_revision_fails(self, tmp_path):
        results = _run(
            tmp_path,
            HF_IMPORT + 'ds = hf_dataset(path="allenai/ai2_arc", split="test", revision=None)\n',
        )
        assert [r.status for r in results] == ["fail"]
        assert "without revision=" in results[0].message

    def test_branch_revision_fails(self, tmp_path):
        results = _run(
            tmp_path,
            HF_IMPORT + 'ds = hf_dataset(path="allenai/ai2_arc", split="test", revision="main")\n',
        )
        assert [r.status for r in results] == ["fail"]
        assert "not a commit SHA" in results[0].message
        assert "'main'" in results[0].message

    def test_short_sha_fails(self, tmp_path):
        results = _run(
            tmp_path,
            HF_IMPORT
            + 'ds = hf_dataset(path="allenai/ai2_arc", split="test", revision="2bb88ca")\n',
        )
        assert [r.status for r in results] == ["fail"]
        assert "not a commit SHA" in results[0].message

    def test_full_sha_passes(self, tmp_path):
        results = _run(
            tmp_path,
            HF_IMPORT
            + f'ds = hf_dataset(path="allenai/ai2_arc", split="test", revision="{SHA}")\n',
        )
        assert [r.status for r in results] == ["pass"]
        assert "1 dataset load(s)" in results[0].message

    def test_revision_from_a_name_passes(self, tmp_path):
        results = _run(
            tmp_path,
            HF_IMPORT
            + 'ds = hf_dataset(path="allenai/ai2_arc", split="test", revision=REVISION)\n'
            + 'other = hf_dataset(path="org/name", split="test", revision=CONFIG["rev"])\n',
        )
        assert [r.status for r in results] == ["pass"]
        assert "2 dataset load(s)" in results[0].message

    def test_datasets_load_dataset_is_checked(self, tmp_path):
        results = _run(
            tmp_path,
            "from datasets import load_dataset\n"
            'ds = load_dataset("allenai/ai2_arc", split="test")\n',
        )
        assert [r.status for r in results] == ["fail"]
        assert "load_dataset()" in results[0].message

    def test_aliased_loader_is_checked(self, tmp_path):
        results = _run(
            tmp_path,
            'from datasets import load_dataset as ld\nds = ld("allenai/ai2_arc", split="test")\n',
        )
        assert [r.status for r in results] == ["fail"]

    def test_module_attribute_loader_is_checked(self, tmp_path):
        results = _run(
            tmp_path,
            'import datasets\nds = datasets.load_dataset("allenai/ai2_arc", split="test")\n',
        )
        assert [r.status for r in results] == ["fail"]

    def test_snapshot_download_is_checked(self, tmp_path):
        results = _run(
            tmp_path,
            "from huggingface_hub import snapshot_download\n"
            'snapshot_download(repo_id="allenai/ai2_arc")\n',
        )
        assert [r.status for r in results] == ["fail"]
        assert "snapshot_download()" in results[0].message
        assert results[0].key == "allenai/ai2_arc"

    def test_local_wrapper_is_not_checked(self, tmp_path):
        results = _run(
            tmp_path,
            'from .dataset import load_dataset\nds = load_dataset("allenai/ai2_arc")\n',
        )
        assert [r.status for r in results] == ["skip"]

    def test_method_named_like_a_loader_is_not_checked(self, tmp_path):
        results = _run(
            tmp_path,
            "class Loader:\n"
            "    def load_dataset(self):\n"
            "        return []\n"
            "\n"
            "Loader().load_dataset()\n",
        )
        assert [r.status for r in results] == ["skip"]

    def test_raw_github_main_url_fails(self, tmp_path):
        url = "https://raw.githubusercontent.com/org/name/main/data/x.jsonl"
        results = _run(tmp_path, f'URL = "{url}"\n')
        assert [r.status for r in results] == ["fail"]
        assert "'main'" in results[0].message
        assert results[0].key == url

    def test_raw_github_master_url_fails(self, tmp_path):
        results = _run(
            tmp_path,
            'URL = "https://raw.githubusercontent.com/org/name/master/data/x.jsonl"\n',
        )
        assert [r.status for r in results] == ["fail"]
        assert "'master'" in results[0].message

    def test_raw_github_sha_url_passes(self, tmp_path):
        results = _run(
            tmp_path,
            f'URL = "https://raw.githubusercontent.com/org/name/{SHA}/data/x.jsonl"\n',
        )
        assert [r.status for r in results] == ["pass"]
        assert "1 raw asset URL(s)" in results[0].message

    def test_url_in_a_docstring_is_not_an_asset(self, tmp_path):
        results = _run(
            tmp_path,
            'def f():\n    """See https://raw.githubusercontent.com/org/name/main/spec.pdf."""\n',
        )
        assert [r.status for r in results] == ["skip"]

    def test_nothing_to_check_skips(self, tmp_path):
        results = _run(tmp_path, "x = 1\n")
        assert [r.status for r in results] == ["skip"]

    def test_one_finding_per_site(self, tmp_path):
        results = _run(
            tmp_path,
            HF_IMPORT
            + 'a = hf_dataset(path="org/one", split="test")\n'
            + f'b = hf_dataset(path="org/two", split="test", revision="{SHA}")\n'
            + 'c = hf_dataset(path="org/three", split="test", revision="dev")\n',
        )
        assert [(r.status, r.line) for r in results] == [("fail", 2), ("fail", 4)]
