"""The credential check that runs before every publish to the public RCHENLAB/AiScientist mirror."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("publish_public_mirror",
                                               ROOT / "scripts" / "publish_public_mirror.py")
pm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pm)


def _tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


# Credential-shaped strings are assembled at run time so that this file passes the same scan.
def test_scan_finds_credentials(tmp_path):
    findings = pm.scan(_tree(tmp_path, {
        "docs/key.md": "export ANTHROPIC_API_KEY=" + "sk-ant-" + "api03-" + "Ab1" * 12 + "\n",
        "deploy/app.env": "BIOAGENT_SMTP_" + "PASSWORD=hunter2hunter2\n",
        "docs/db.md": "postgresql+psycopg://bioagent:" + "s3cretpw" + "@db:5432/bioagent\n",
        "notes/id.txt": "-----BEGIN OPENSSH " + "PRIVATE KEY-----\n",
    }))
    assert any("Anthropic API key" in f for f in findings)
    assert any("BIOAGENT_SMTP_PASSWORD" in f for f in findings)
    assert any("password in a URL" in f for f in findings)
    assert any("private key" in f for f in findings)


def test_scan_passes_placeholders_code_and_test_fixtures(tmp_path):
    assert pm.scan(_tree(tmp_path, {
        "configs/example.env": (
            "BIOAGENT_DATABASE_URL=postgresql+psycopg://bioagent:<password>@localhost:5432/bioagent\n"
            "BIOAGENT_LLM_API_KEY=local-dev-key\n"
            "OPENROUTER_API_KEY=sk-or-...\n"
            "BIOAGENT_SECRET_KEY=…\n"),
        "deploy/sudoers.example": "bioagent ALL=(root) NOPASSWD: /usr/bin/systemctl restart bioagent\n",
        "scripts/gate.py": 'ENV_API_KEY_PATTERN = re.compile(r"API_KEY=")\n',
        "tests/test_login.py": "BIOAGENT_ADMIN_" + "PASSWORD=rootpass1\n",
        "tests/test_aws.py": "here is my key AKIAIOSFODNN7EXAMPLE\n",
    })) == []
