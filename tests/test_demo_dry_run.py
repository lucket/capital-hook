"""Runs the whole DEMO suite against the fake (no network), so the harness is proven before it touches a real account."""
import os
import subprocess
import sys
from pathlib import Path

import demo_harness as h

ROOT = Path(__file__).resolve().parent.parent


def test_the_demo_suite_passes_end_to_end_against_the_fake(tmp_path):
    env = {**os.environ, h.DRY_RUN_ENV: "1", "CAPITAL_DEMO_EPIC": "GOLD",
           "CAPITAL_DEMO_OBSERVED_PATH": str(tmp_path / "observed.json"), "CAPITAL_DEMO_JOURNAL_PATH": str(tmp_path / "journal.json"),
           "CAPITAL_DEMO_SHAPES_PATH": str(tmp_path / "shapes.json"), "CAPITAL_DEMO_WRITE_SHAPES": "1"}
    for name in h.REQUIRED_ENV:
        env.pop(name, None)                                                       # prove it does not need credentials
    run = subprocess.run([sys.executable, "-m", "pytest", "-m", "demo", "tests/integration", "-q", "-p", "no:cacheprovider"],
                         cwd=ROOT, env=env, capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "10 passed" in run.stdout and "skipped" not in run.stdout
    assert (tmp_path / "observed.json").exists() and (tmp_path / "shapes.json").exists()
    assert (tmp_path / "journal.json").read_text() == "[]"                        # nothing left behind


def test_without_the_opt_in_the_demo_suite_skips_everything(tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in (*h.REQUIRED_ENV, h.ALLOW_ENV, h.DRY_RUN_ENV)}
    env["CAPITAL_DEMO_OBSERVED_PATH"] = str(tmp_path / "observed.json")
    run = subprocess.run([sys.executable, "-m", "pytest", "-m", "demo", "tests/integration", "-q", "-p", "no:cacheprovider"],
                         cwd=ROOT, env=env, capture_output=True, text=True)
    assert run.returncode == 0 and "10 skipped" in run.stdout and "passed" not in run.stdout
