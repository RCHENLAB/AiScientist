"""The retained worker supports handoff/platform tasks and refuses retired science."""
import json
import pytest
from aiscientist.tools import worker_cli

@pytest.mark.parametrize("tool", ["run_scanpy_qc", "run_clustering", "run_cellqc", "run_de"])
def test_scientific_dispatch_is_retired(tool):
    assert worker_cli.run_tool(tool, "/unused", None, {})["status"] == "error"

def test_preflight_and_marked_cli_contract(monkeypatch, capsys):
    seen = {}
    def smoke(path, output):
        seen.update(dataset=str(path), output=str(output))
        return {"status": "ok", "metadata": True}
    monkeypatch.setattr("aiscientist.tools._lib.dataset_preflight.run_dataset_preflight", smoke)
    assert worker_cli.main(["--tool", "preflight", "--workspace", "/run", "--dataset", "/input.h5ad"]) == 0
    assert seen == {"dataset": "/input.h5ad", "output": "/run/artifacts/data"}
    assert json.loads(capsys.readouterr().out.removeprefix(worker_cli.RESULT_MARKER))["metadata"] is True

@pytest.mark.parametrize("operation,module,function", [
    (worker_cli.INSTALL_DEPENDENCY, "aiscientist.tools.run_deps", "install"),
    (worker_cli.EXPORT_RESULT_DATASET, "aiscientist.tools._lib.result_dataset", "export"),
    (worker_cli.ADOPT_RESULT_DATASET, "aiscientist.tools._lib.result_dataset", "adopt"),
])
def test_platform_operation_remains_reachable(monkeypatch, operation, module, function):
    import importlib
    called = []
    monkeypatch.setattr(importlib.import_module(module), function,
                        lambda *args: called.append(args) or {"status": "ok"})
    assert worker_cli.run_tool(operation, "/run", "/input.h5ad", {"dependency": "pydeseq2"}) == {"status": "ok"}
    assert called and called[0][0] == "/run"
