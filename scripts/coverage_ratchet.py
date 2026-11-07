"""Coverage ratchet (ROADMAP P1): the suite's total coverage may never decrease.

Usage:
    .venv\\Scripts\\python.exe scripts/coverage_ratchet.py          # check only (CI)
    .venv\\Scripts\\python.exe scripts/coverage_ratchet.py --update  # ratchet up locally

Runs the hardware-independent suite under coverage, compares the total
against scripts/coverage_baseline.json, and fails when below it. With
--update a HIGHER total rewrites the baseline (ratchet up only — never down).

Deliberately a ratchet, not a cliff: no --cov-fail-under=N (unrealistic for
Windows/hardware-heavy code, trains junk tests).
"""

from __future__ import annotations

import contextlib
import datetime
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "scripts" / "coverage_baseline.json"
COVERAGE_JSON = ROOT / "coverage.json"

PACKAGES = ["core", "api", "skills", "voice", "utils"]
# Hardware-dependent suites excluded everywhere (see README "Tests & drills").
IGNORES = [
    "tests/test_voice.py",
    "tests/test_voice2.py",
    "tests/test_voice_distiller.py",
    "tests/test_vision2.py",
]


def run_suite() -> None:
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "tests/",
        "-q",
        "-p",
        "no:cacheprovider",
        *(f"--cov={p}" for p in PACKAGES),
        "--cov-report=json:coverage.json",
        "--cov-report=",
        *[a for i in IGNORES for a in ("--ignore", i)],
    ]
    print("+", " ".join(cmd), flush=True)
    proc = subprocess.run(cmd, cwd=ROOT)
    if proc.returncode != 0:
        print("coverage_ratchet: test suite FAILED — fix tests before reading coverage")
        sys.exit(1)


def read_total() -> float:
    data = json.loads(COVERAGE_JSON.read_text(encoding="utf-8"))
    return float(data["totals"]["percent_covered"])


def read_baseline() -> dict:
    if BASELINE.exists():
        return json.loads(BASELINE.read_text(encoding="utf-8"))
    return {"total": 0.0, "updated": "none"}


def main() -> None:
    update = "--update" in sys.argv[1:]
    run_suite()
    total = read_total()
    base = read_baseline()
    print(f"coverage_ratchet: total={total:.2f}% baseline={base['total']:.2f}%")
    if total + 1e-9 < base["total"]:
        print(
            f"coverage_ratchet: FAIL — coverage decreased "
            f"({base['total']:.2f}% -> {total:.2f}%). Add tests, then re-run."
        )
        sys.exit(1)
    if update and total > base["total"]:
        BASELINE.write_text(
            json.dumps(
                {
                    "total": round(total, 2),
                    "updated": datetime.date.today().isoformat(),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"coverage_ratchet: baseline ratcheted up to {total:.2f}%")
    else:
        print("coverage_ratchet: PASS")
    with contextlib.suppress(OSError):
        COVERAGE_JSON.unlink()


if __name__ == "__main__":
    main()
