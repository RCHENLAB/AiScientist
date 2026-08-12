"""Offline tests for Phase 5: report render (pandoc/xelatex) as an HPC3 Slurm job.

A scripted fake RemoteExecutor drives submit -> run -> collect; its get_file simulates pulling the
produced PDF back. Also covers the in-process fallback and that build_pdf_report uses an injected
renderer (so pandoc need not be installed on the eyeserver). No real Slurm, no pandoc, no texlive.
"""

from __future__ import annotations

from pathlib import Path

from bioagent.gateway.errors import GatewayError
from bioagent.gateway.executor import ExecResult
from bioagent.gateway.slurm_report import SlurmReportRenderer
from bioagent.tools import report


class FakeHPC:
    def __init__(self, sacct: str = "COMPLETED"):
        self.host, self.username = "hpc3-mock", "tester"
        self.sacct = sacct
        self.submits: list[str] = []
        self.all_cmds: list[str] = []
        self._next = 900

    def _ok(self, out=""):
        return ExecResult(command="", exit_status=0, stdout=out, stderr="")

    # Contents the fake job "log" tail returns — a stand-in for a real xelatex error.
    log_body = "! LaTeX Error: File `figures/umap.png' not found.\nl.42 ...more xelatex noise"

    def exec(self, command, timeout=60.0):
        cmd = command.strip()
        self.all_cmds.append(cmd)
        if cmd.startswith("sbatch"):
            jid = str(self._next); self._next += 1
            self.submits.append(jid)
            return self._ok(f"Submitted batch job {jid}")
        if cmd.startswith("squeue"):
            return self._ok("")
        if cmd.startswith("sacct"):
            return self._ok(self.sacct)
        if cmd.startswith("tail"):
            return self._ok(self.log_body)      # the failure-diagnosis log fetch
        return self._ok()                       # mkdir / tar / etc.

    def put_file(self, *a, **k):
        pass

    def get_file(self, remote_path, local_path):
        Path(local_path).write_bytes(b"%PDF-1.4 fake render\n")   # simulate the produced output

    def open_tunnel(self, *a, **k):
        return 1

    def close(self):
        pass


def test_report_render_on_slurm_stages_remaps_and_pulls_back(tmp_path):
    bundle = tmp_path / "run" / "artifacts"
    (bundle / "figures").mkdir(parents=True)
    (bundle / "report").mkdir()
    md = bundle / "report" / "report.md"
    md.write_text("# hi")
    out = bundle / "report" / "report.pdf"
    cmd = ["pandoc", str(md), "-o", str(out), "--resource-path", str(bundle), "--pdf-engine=xelatex"]

    hpc = FakeHPC()
    r = SlurmReportRenderer(remote=hpc, container_image="/dfs/report.sif",
                            remote_base="/dfs/lab/tester/reports", startup_timeout_s=5, run_timeout_s=5)
    ok, err = r(cmd, bundle, out, 60.0)

    assert ok and err == "" and out.exists()
    assert len(hpc.submits) == 1
    joined = "\n".join(hpc.all_cmds)
    assert "/dfs/lab/tester/reports" in joined      # bundle staged + cmd remapped to the remote dir
    assert "report.pdf" in joined


class FakeHPCNoOutput(FakeHPC):
    """A cluster where the render job runs but writes NO PDF (the pandoc/xelatex failure case):
    ``get_file`` raises ``GatewayError`` — a RuntimeError, exactly like the real SFTP get on a
    missing remote file — which used to escape and crash the whole run."""

    def get_file(self, remote_path, local_path):
        raise GatewayError(f"Failed to download {remote_path} -> {local_path}.", stage="ssh_get")


def test_report_render_missing_output_is_diagnosable_and_never_throws(tmp_path):
    # No PDF produced -> renderer must (a) NOT raise, (b) surface the Slurm log tail so the real
    # cause is visible instead of an opaque "no output".
    bundle = tmp_path / "run" / "artifacts"
    (bundle / "report").mkdir(parents=True)
    out = bundle / "report" / "report.pdf"
    cmd = ["pandoc", "in.md", "-o", str(out), "--pdf-engine=xelatex"]

    hpc = FakeHPCNoOutput()
    r = SlurmReportRenderer(remote=hpc, container_image="/img", remote_base="/dfs/lab/tester/reports",
                            startup_timeout_s=5, run_timeout_s=5)
    ok, err = r(cmd, bundle, out, 60.0)   # must return, not raise

    assert ok is False and not out.exists()
    assert "umap.png' not found" in err            # the real xelatex error made it into the message
    assert any(c.startswith("tail") for c in hpc.all_cmds)


def test_report_render_missing_output_falls_back_to_local(tmp_path):
    # When HPC produces no PDF but a local pandoc is wired, the render degrades to it (rather than
    # failing outright) — the local fallback now fires on a completed-but-empty render, not only on
    # a SlurmJobError.
    bundle = tmp_path / "run" / "artifacts"
    (bundle / "report").mkdir(parents=True)
    out = bundle / "report" / "report.pdf"

    def local(cmd, cwd, out_path, timeout_s):
        Path(out_path).write_bytes(b"%PDF local\n")
        return (True, "")

    hpc = FakeHPCNoOutput()
    r = SlurmReportRenderer(remote=hpc, container_image="/img", remote_base="/x",
                            startup_timeout_s=5, run_timeout_s=5, local_fallback=local)
    ok, err = r(["pandoc", "-o", str(out)], bundle, out, 60.0)

    assert ok is True and err == "" and out.exists()


class FakeHPCMkdirTimeout(FakeHPC):
    """The reported production bug: the FIRST remote op — ``mkdir -p <reports> <scratch>`` — hangs on
    a slow DFS and the SSH exec raises ``GatewayError("Command timed out after 60s: mkdir ...")``
    mid-render. A GatewayError is NOT a SlurmJobError, so it used to ESCAPE the renderer and surface
    as a fatal "Chat error" that discarded an already-completed run (analysis done, only render slow)."""

    def exec(self, command, timeout=60.0):
        if command.strip().startswith("mkdir"):
            raise GatewayError(f"Command timed out after {timeout:.0f}s: {command}", stage="ssh_exec")
        return super().exec(command, timeout)


def test_report_render_mkdir_timeout_never_throws(tmp_path):
    # A slow DFS mkdir that times out (GatewayError, not SlurmJobError) must NOT escape as a fatal
    # error — the renderer returns (False, err) so build_pdf_report degrades (markdown-only) and the
    # completed run still ships. Regression for the "Chat error: Command timed out after 60s: mkdir".
    bundle = tmp_path / "run" / "artifacts"
    (bundle / "report").mkdir(parents=True)
    out = bundle / "report" / "report.pdf"
    r = SlurmReportRenderer(remote=FakeHPCMkdirTimeout(), container_image="/img",
                            remote_base="/dfs/lab/tester/reports", startup_timeout_s=5, run_timeout_s=5)
    ok, err = r(["pandoc", "-o", str(out)], bundle, out, 60.0)   # must return, not raise
    assert ok is False and "timed out" in err and not out.exists()


def test_report_render_mkdir_timeout_falls_back_to_local(tmp_path):
    # Same timeout, but with a local pandoc wired: any remote failure (not just a SlurmJobError) now
    # degrades to the local renderer, so the user still gets their report.
    bundle = tmp_path / "run" / "artifacts"
    (bundle / "report").mkdir(parents=True)
    out = bundle / "report" / "report.pdf"

    def local(cmd, cwd, out_path, timeout_s):
        Path(out_path).write_bytes(b"%PDF local\n")
        return (True, "")

    r = SlurmReportRenderer(remote=FakeHPCMkdirTimeout(), container_image="/img", remote_base="/x",
                            startup_timeout_s=5, run_timeout_s=5, local_fallback=local)
    ok, err = r(["pandoc", "-o", str(out)], bundle, out, 60.0)
    assert ok is True and err == "" and out.exists()


class FakeHPCEcho(FakeHPC):
    """A login shell that resolves ``$HOME``: ``echo $HOME/.bioagent/report`` -> an absolute path."""

    def exec(self, command, timeout=60.0):
        if command.strip().startswith("echo "):
            return self._ok("/home/tester/.bioagent/report")
        return super().exec(command, timeout)


def test_report_render_resolves_home_no_literal_in_job(tmp_path):
    # Regression: the render Slurm job put a raw $HOME/.bioagent/report in `#SBATCH --output` AND the
    # `singularity -B` bind. Slurm doesn't expand $HOME in --output, and the quoted bind froze it, so
    # every HPC render died instantly (exit 127) — no report ever rendered. With a shell that resolves
    # $HOME, the ABSOLUTE path must be baked into the submitted job, with no literal "$HOME" anywhere.
    bundle = tmp_path / "run" / "artifacts"
    (bundle / "report").mkdir(parents=True)
    md = bundle / "report" / "report.md"; md.write_text("# hi")
    out = bundle / "report" / "report.pdf"
    cmd = ["pandoc", str(md), "-o", str(out), "--pdf-engine=xelatex"]

    hpc = FakeHPCEcho()
    r = SlurmReportRenderer(remote=hpc, container_image="/img", remote_base="/dfs/lab/tester/reports",
                            startup_timeout_s=5, run_timeout_s=5)
    ok, err = r(cmd, bundle, out, 60.0)

    assert ok and err == "" and out.exists()
    joined = "\n".join(hpc.all_cmds)
    assert "$HOME" not in joined                        # no unexpanded $HOME leaked into any command
    assert "/home/tester/.bioagent/report" in joined    # the resolved absolute scratch was used


def test_report_render_no_remote_falls_back(tmp_path):
    calls = {}

    def fake_local(cmd, cwd, out_path, timeout_s):
        calls["cmd"] = cmd
        return (True, "")

    r = SlurmReportRenderer(remote=None, container_image="/img", remote_base="/x",
                            local_fallback=fake_local)
    ok, _err = r(["pandoc"], tmp_path, tmp_path / "o.pdf", 10.0)
    assert ok and calls["cmd"] == ["pandoc"]


def test_build_pdf_report_uses_injected_renderer(tmp_path):
    # render_fn given -> pandoc need NOT be installed locally; the injected renderer is called.
    calls: list[str] = []

    def fake_render(cmd, cwd, out_path, timeout_s):
        Path(out_path).write_bytes(b"x")
        calls.append(Path(out_path).name)
        return (True, "")

    res = report.build_pdf_report("# Hello", tmp_path, basename="report",
                                  formats=("pdf", "docx"), render_fn=fake_render)
    assert res["status"] == "ok"
    assert calls == ["report.pdf", "report.docx"]
    assert res["pdf_path"] and res["docx_path"]
