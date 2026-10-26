"""The upload cards' speed and time left (RCHENLAB/AiScientist issue 1).

The arithmetic lives in the console script; this runs that block under node, with the page's globals
stubbed. Skipped where node is not installed.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[1] / "frontend" / "console" / "app.js"
START = "// -- speed and time left"
END = "const uploadDigestCache"

HARNESS = r"""
const vm = require("vm");
const src = require("fs").readFileSync(0, "utf8");
const sandbox = { state: { uploads: [] }, renders: 0, timers: 0,
  renderUploads() { sandbox.renders++; }, setInterval() { sandbox.timers++; return 1; },
  clearInterval() {}, Date, Math, Map };
const api = vm.runInNewContext(src +
  "\n;({ fmtUploadSpeed, fmtUploadEta, noteUploadPace, uploadPaceOf, uploadPaceText, uploadPaces })", sandbox);
const out = {};
out.eta = [0.4, 59, 90, 3570, 3600, 5400, 86400].map(api.fmtUploadEta);
out.speed = [500, 850e3, 35.2e6, 1.5e9].map(api.fmtUploadSpeed);

// a steady 50 MB/s upload of 1 GB
const u = { id: "a", status: "uploading", size: 1000e6, sent: 0 };
api.noteUploadPace(u, 0);
out.one_sample = api.uploadPaceOf(u, 0);
u.sent = 20e6; api.noteUploadPace(u, 400);
out.too_early = api.uploadPaceOf(u, 400);
u.sent = 50e6; api.noteUploadPace(u, 1000);
u.sent = 100e6; api.noteUploadPace(u, 2000);
out.steady = api.uploadPaceOf(u, 2000);
out.stalled = api.uploadPaceOf(u, 2000 + 20001);

// the link slows down: the speed follows the last 15 s, not the whole upload
const v = { id: "b", status: "uploading", size: 10000e6, sent: 0 };
for (let t = 0; t <= 20; t++) { v.sent = t * 100e6; api.noteUploadPace(v, t * 1000); }      // 100 MB/s
for (let t = 21; t <= 40; t++) { v.sent = 2000e6 + (t - 20) * 10e6; api.noteUploadPace(v, t * 1000); }   // 10 MB/s
out.slowed = api.uploadPaceOf(v, 40000);

// the copy to HPC3 counts from 0 again: a new measurement
u.status = "staging"; u.sent = 0; api.noteUploadPace(u, 3000);
out.new_phase = api.uploadPaceOf(u, 3000);
u.sent = 300e6; api.noteUploadPace(u, 5000);
sandbox.Date = { now: () => 5000 };
out.copy_text = vm.runInNewContext("f(u)", { f: api.uploadPaceText, u });
out.copy = api.uploadPaceOf(u, 5000);

// a finished card is no longer measured
u.status = "done"; api.noteUploadPace(u, 6000);
out.tracked_after_done = api.uploadPaces.has("a");
out.timers = sandbox.timers;
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def pace():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    source = APP_JS.read_text(encoding="utf-8")
    block = source[source.index(START):source.index(END)]
    done = subprocess.run([node, "-e", HARNESS], input=block, capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_time_left_reads_in_the_unit_that_fits(pace):
    assert pace["eta"] == ["1 s left", "59 s left", "2 min left", "1 h left", "1 h left",
                           "1 h 30 min left", "24 h left"]


def test_speed_reads_in_the_unit_that_fits(pace):
    assert pace["speed"] == ["1 kB/s", "850 kB/s", "35.2 MB/s", "1.50 GB/s"]


def test_a_steady_upload_reports_its_speed_and_time_left(pace):
    assert pace["steady"]["bps"] == pytest.approx(50e6)
    assert pace["steady"]["eta"] == pytest.approx(18.0)        # 900 MB left at 50 MB/s


def test_nothing_is_reported_before_a_second_of_progress_or_after_a_stall(pace):
    assert pace["one_sample"] is None and pace["too_early"] is None
    assert pace["stalled"] is None


def test_the_speed_follows_a_link_that_slowed_down(pace):
    # 100 MB/s for 20 s, then 10 MB/s for 20 s: an average since the start would say 55 MB/s
    assert pace["slowed"]["bps"] == pytest.approx(10e6)
    assert pace["slowed"]["eta"] == pytest.approx((10000e6 - 2200e6) / 10e6)


def test_the_copy_to_hpc3_is_measured_on_its_own(pace):
    assert pace["new_phase"] is None
    assert pace["copy"]["bps"] == pytest.approx(150e6)
    assert pace["copy_text"] == " · 150.0 MB/s · 5 s left"
    assert pace["tracked_after_done"] is False
    assert pace["timers"] == 1                                 # one repaint timer, however many cards


def test_the_closing_message_of_a_folder_says_what_was_sent():
    """A folder chosen again is not sent again (RCHENLAB/AiScientist issue 1): the message that
    closes its card says how much of it was added, or that nothing was."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    source = APP_JS.read_text(encoding="utf-8")
    size = next(line for line in source.splitlines() if line.startswith("const fmtUploadSize"))
    start = source.index("function folderDoneText(d) {")
    block = source[start:source.index("\n}\n", start) + 3]
    cases = [{"name": "run1", "count": 41, "size": 16.17e9},
             {"name": "run1", "count": 41, "size": 16.17e9, "kept": 6},
             {"name": "run1", "count": 41, "size": 16.17e9, "kept": 41},
             {"name": "run1", "count": 41, "size": 16.17e9, "already": True}]
    script = f"{size}\n{block}\nconsole.log(JSON.stringify({json.dumps(cases)}.map(folderDoneText)))"
    done = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == [
        "Uploaded folder run1/ (41 files, 16.17 GB)",
        "Added 35 files to run1/: now 41 files, 16.17 GB",
        "run1/ was already uploaded (41 files, 16.17 GB): nothing was sent again",
        "run1/ was already uploaded (41 files, 16.17 GB): nothing was sent again",
    ]
