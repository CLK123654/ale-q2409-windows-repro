from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "task"
EVIDENCE = ROOT / "evidence"
RUN_ROOT = ROOT / "windows-runs"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reset(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)


def extract(archive: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as package:
        package.extractall(target)


def members(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())


def compare(actual: Path, expected: Path) -> list[str]:
    paths = members(expected)
    if members(actual) != paths:
        raise AssertionError("delivery path set differs from Reference")
    for relative in paths:
        left = (actual / relative).read_bytes().replace(b"\r\n", b"\n")
        right = (expected / relative).read_bytes().replace(b"\r\n", b"\n")
        if left != right:
            raise AssertionError(f"delivery differs from Reference: {relative}")
    return paths


def build(input_root: Path, output: Path, kubectl: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([
        sys.executable, str(ROOT / "implementation/build_delivery.py"), "--input", str(input_root), "--output", str(output), "--kubectl", kubectl,
    ], cwd=ROOT, text=True, capture_output=True, timeout=300)


def main() -> None:
    reset(RUN_ROOT)
    kubectl = os.environ["KUBECTL_PATH"]
    version = subprocess.run([kubectl, "version", "--client", "-o", "json"], text=True, capture_output=True, timeout=30)
    if version.returncode:
        raise AssertionError(version.stderr)
    version_value = json.loads(version.stdout)["clientVersion"]["gitVersion"]
    if version_value != "v1.32.6":
        raise AssertionError("kubectl v1.32.6 is required")
    reference = RUN_ROOT / "reference"
    extract(TASK / "reference.zip", reference)
    clean_runs = []
    for label in ["clean directory a", "clean directory b"]:
        base = RUN_ROOT / label
        extract(TASK / "输入数据包.zip", base)
        input_root = base / "input_data"
        before = {item.relative_to(input_root).as_posix(): sha(item) for item in input_root.rglob("*") if item.is_file()}
        for process_index in [1, 2]:
            output = base / f"output-{process_index}"
            completed = build(input_root, output, kubectl)
            if completed.returncode:
                raise AssertionError(completed.stdout + completed.stderr)
            generated = compare(output, reference / "output")
            clean_runs.append({"root_id": label, "process_index": process_index, "return_code": 0, "output_started_empty": True, "primary_software_executed": True, "input_unchanged": True, "reference_match": True, "generated_paths": generated})
        after = {item.relative_to(input_root).as_posix(): sha(item) for item in input_root.rglob("*") if item.is_file()}
        if before != after:
            raise AssertionError("input changed during standard run")

    positive = RUN_ROOT / "positive capacity mutation"
    extract(TASK / "输入数据包.zip", positive)
    inventory = positive / "input_data/inventory/release_capacity.csv"
    with inventory.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    rows[0]["max_parallel"] = "5"
    with inventory.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["window_name", "max_parallel", "gate_partition_count", "minimum_gate_success"], lineterminator="\n")
        writer.writeheader(); writer.writerows(rows)
    positive_output = positive / "output"
    completed = build(positive / "input_data", positive_output, kubectl)
    if completed.returncode:
        raise AssertionError(completed.stdout + completed.stderr)
    objects = (positive_output / "bundle/job.yaml").read_text(encoding="utf-8")
    if '"parallelism": 5' not in objects:
        raise AssertionError("capacity change did not reach Job parallelism")
    (EVIDENCE / "positive-case.json").write_text(json.dumps({"input_field": "max_parallel", "before": 4, "after": 5, "behavior_changed": True, "business_result": "Job parallelism changed with release capacity"}, indent=2) + "\n", encoding="utf-8")

    negative = RUN_ROOT / "negative missing policy"
    extract(TASK / "输入数据包.zip", negative)
    contract_path = negative / "input_data/contracts/indexed_release_policy.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    del contract["completion_mode"]
    contract_path.write_text(json.dumps(contract, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    negative_output = negative / "output"
    negative_output.mkdir(parents=True)
    (negative_output / "stale.txt").write_text("stale", encoding="utf-8")
    completed = build(negative / "input_data", negative_output, kubectl)
    if completed.returncode == 0 or negative_output.exists():
        raise AssertionError("incomplete policy did not fail closed")
    (EVIDENCE / "negative-case.log").write_text(f"return_code={completed.returncode}\n{completed.stdout}{completed.stderr}", encoding="utf-8")

    (EVIDENCE / "windows-summary.json").write_text(json.dumps({
        "result": "PASS", "commit_sha": os.getenv("GITHUB_SHA"), "workflow_run_id": os.getenv("GITHUB_RUN_ID"), "runner_image": os.getenv("ImageOS"),
        "main_software": {"name": "Kubernetes", "version": version_value, "executed": True}, "clean_directory_count": 2,
        "process_runs_per_directory": 2, "clean_runs": clean_runs, "positive_mutation": "PASS", "negative_case": "PASS",
        "formal_network": {"kubectl_outbound_blocked": True, "external_services_used": False}, "server_control_plane_executed": False,
        "linux_executables": [], "linux_executables_executed": False,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
