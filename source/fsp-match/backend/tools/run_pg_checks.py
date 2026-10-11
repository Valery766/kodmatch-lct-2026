"""Run real PostgreSQL checks without exposing assertion inputs or DSNs."""
import importlib.metadata
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET


def main():
    if not os.environ.get("FSP_TEST_DATABASE_URL", ""):
        print(json.dumps({"status": "failed", "reason": "dedicated_postgres_url_missing"}))
        return 2
    target = "/tmp/fsp-pg-results.xml"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_postgres_integration.py", "-q", "--tb=no", "-rN", "--disable-warnings", "-p", "no:cacheprovider", "--junitxml=" + target],
        cwd="/app", capture_output=True, text=True, timeout=240,
    )
    report = {"status": "failed", "exit_code": result.returncode, "pytest": importlib.metadata.version("pytest"), "python": sys.version.split()[0]}
    # Raw stdout/stderr may contain credentials in third-party exception text.
    # Only JUnit counters and fixed test identifiers leave this container.
    try:
        suites = ET.parse(target).getroot()
        counters = {key: sum(int(suite.get(key, "0")) for suite in suites.iter("testsuite")) for key in ["tests", "failures", "errors", "skipped"]}
        report.update(counters)
        report["passed"] = counters["tests"] - counters["failures"] - counters["errors"] - counters["skipped"]
        report["checks"] = [{"name": case.get("name"), "status": "failed" if case.find("failure") is not None or case.find("error") is not None else "skipped" if case.find("skipped") is not None else "passed"} for case in suites.iter("testcase")]
        if result.returncode == 0 and report["passed"] == 3 and counters["skipped"] == 0:
            report["status"] = "passed"
    except (OSError, ET.ParseError, ValueError):
        report["reason"] = "test_result_unavailable"
    print(json.dumps(report))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print(json.dumps({"status": "failed", "reason": "test_runner_failed"}))
        sys.exit(1)
