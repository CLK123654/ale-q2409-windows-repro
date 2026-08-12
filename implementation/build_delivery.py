from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
from pathlib import Path


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def dump_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(command: list[str]) -> str:
    completed = subprocess.run(command, capture_output=True, timeout=120)
    if completed.returncode:
        raise RuntimeError((completed.stdout + completed.stderr).decode("utf-8", errors="replace"))
    return completed.stdout.decode("utf-8", errors="strict").replace("\r\n", "\n")


def write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader(); writer.writerows(rows)


def resource(api_version: str, kind: str, name: str, namespace: str | None = None, body: dict | None = None) -> dict:
    metadata = {"name": name}
    if namespace:
        metadata["namespace"] = namespace
    value = {"apiVersion": api_version, "kind": kind, "metadata": metadata}
    if body:
        value.update(body)
    return value


def inspect_manifest(manifest: Path, kubectl: str) -> list[dict]:
    documents = [item.strip() for item in manifest.read_text(encoding="utf-8").split("\n---\n") if item.strip()]
    temp = manifest.parent / ".decode"
    temp.mkdir()
    parsed: list[dict] = []
    try:
        for index, document in enumerate(documents, start=1):
            source = temp / f"object-{index}.yaml"
            source.write_text(document + "\n", encoding="utf-8")
            item = json.loads(run([kubectl, "patch", "--local", "-f", str(source), "--type", "merge", "-p", "{}", "-o", "json"]))
            parsed.extend(item.get("items", []) if item.get("kind") == "List" else [item])
    finally:
        shutil.rmtree(temp)
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--kubectl", required=True)
    args = parser.parse_args()
    input_root, output = Path(args.input).resolve(), Path(args.output).resolve()
    if output.exists():
        shutil.rmtree(output)
    try:
        contract = load_json(input_root / "contracts/indexed_release_policy.json")
        required = {"namespace", "job_name", "configmap_name", "image", "completion_mode", "backoff_limit_per_index", "max_failed_indexes", "permanent_error_exit_code", "ignored_pod_condition", "readiness"}
        if set(contract) != required or contract["completion_mode"] != "Indexed":
            raise ValueError("发布合同字段不完整")
        with (input_root / "inventory/catalog_partitions.csv").open(encoding="utf-8", newline="") as handle:
            partitions = list(csv.DictReader(handle))
        with (input_root / "inventory/release_capacity.csv").open(encoding="utf-8", newline="") as handle:
            capacities = list(csv.DictReader(handle))
        if len(capacities) != 1:
            raise ValueError("窗口容量必须只有一条有效记录")
        capacity = capacities[0]
        active = [row for row in partitions if row["enabled"] == "true"]
        gate = [row for row in active if row["tier"] == "gate"]
        if len(gate) != int(capacity["gate_partition_count"]) or int(capacity["minimum_gate_success"]) > len(gate):
            raise ValueError("核心市场范围与窗口容量不一致")
        if len({row["partition_id"] for row in partitions}) != len(partitions):
            raise ValueError("目录清单存在重复主键")
        change_request = (input_root / "change_request.txt").read_text(encoding="utf-8")
        starter = (input_root / "starter/job.yaml").read_text(encoding="utf-8")
        if "周三" not in change_request or "restartPolicy: OnFailure" not in starter or "image: registry.example.invalid/cdn/catalog-check:latest" not in starter:
            raise ValueError("变更说明或starter缺陷入口不完整")

        namespace = resource("v1", "Namespace", contract["namespace"])
        configmap = resource("v1", "ConfigMap", contract["configmap_name"], contract["namespace"], {"data": {
            "partitions.csv": "partition_index,partition_id,region,tier\n" + "\n".join(
                f"{index},{row['partition_id']},{row['region']},{row['tier']}" for index, row in enumerate(active)
            ) + "\n"
        }})
        gate_indexes = list(range(len(gate)))
        gate_range = f"{min(gate_indexes)}-{max(gate_indexes)}" if len(gate_indexes) > 1 else str(gate_indexes[0])
        job = resource("batch/v1", "Job", contract["job_name"], contract["namespace"], {"spec": {
            "completionMode": contract["completion_mode"],
            "completions": len(active),
            "parallelism": int(capacity["max_parallel"]),
            "backoffLimit": 2147483647,
            "backoffLimitPerIndex": contract["backoff_limit_per_index"],
            "maxFailedIndexes": contract["max_failed_indexes"],
            "podFailurePolicy": {"rules": [
                {"action": "FailIndex", "onExitCodes": {"containerName": "checker", "operator": "In", "values": [contract["permanent_error_exit_code"]]}},
                {"action": "Ignore", "onPodConditions": [{"type": contract["ignored_pod_condition"], "status": "True"}]},
            ]},
            "successPolicy": {"rules": [{"succeededIndexes": gate_range, "succeededCount": int(capacity["minimum_gate_success"])}]},
            "template": {"spec": {
                "restartPolicy": "Never",
                "containers": [{
                    "name": "checker", "image": contract["image"],
                    "args": ["--partition-index=$(PARTITION_INDEX)", "--partition-map=/etc/catalog/partitions.csv"],
                    "env": [{"name": "PARTITION_INDEX", "valueFrom": {"fieldRef": {"fieldPath": "metadata.labels['batch.kubernetes.io/job-completion-index']"}}}],
                    "volumeMounts": [{"name": "partition-map", "mountPath": "/etc/catalog", "readOnly": True}],
                    "readinessProbe": {"httpGet": contract["readiness"]},
                }],
                "volumes": [{"name": "partition-map", "configMap": {"name": contract["configmap_name"]}}],
            }},
        }})
        bundle = output / "bundle"
        for filename, value in [("namespace.yaml", namespace), ("configmap.yaml", configmap), ("job.yaml", job)]:
            dump_json(bundle / filename, value)
        dump_json(bundle / "kustomization.yaml", {"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization", "resources": ["namespace.yaml", "configmap.yaml", "job.yaml"]})
        manifest = output / "dist/cdn-catalog-check.yaml"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(run([args.kubectl, "kustomize", str(bundle)]), encoding="utf-8")
        decoded = inspect_manifest(manifest, args.kubectl)
        expected = {(value["kind"], value["metadata"].get("namespace", ""), value["metadata"]["name"]) for value in [namespace, configmap, job]}
        actual = {(value["kind"], value["metadata"].get("namespace", ""), value["metadata"]["name"]) for value in decoded}
        if actual != expected or len(decoded) != len(expected):
            raise ValueError("构建对象集合与发布合同不一致")
        write_csv(output / "reports/objects.csv", ["api_version", "kind", "namespace", "name", "source_contract"], [{
            "api_version": value["apiVersion"], "kind": value["kind"], "namespace": value["metadata"].get("namespace", ""), "name": value["metadata"]["name"], "source_contract": "indexed_release_policy.json",
        } for value in sorted(decoded, key=lambda item: (item["kind"], item["metadata"]["name"]))])
        review = [
            ("active_partitions", len(active), "inventory/catalog_partitions.csv", "ConfigMap data.partitions.csv"),
            ("parallelism", int(capacity["max_parallel"]), "inventory/release_capacity.csv", "Job spec.parallelism"),
            ("completionMode", contract["completion_mode"], "contracts/indexed_release_policy.json", "Job spec.completionMode"),
            ("backoffLimitPerIndex", contract["backoff_limit_per_index"], "contracts/indexed_release_policy.json", "Job spec.backoffLimitPerIndex"),
            ("maxFailedIndexes", contract["max_failed_indexes"], "contracts/indexed_release_policy.json", "Job spec.maxFailedIndexes"),
            ("permanent_error_action", "FailIndex", "contracts/indexed_release_policy.json", "Job spec.podFailurePolicy.rules[0]"),
            ("disruption_action", "Ignore", "contracts/indexed_release_policy.json", "Job spec.podFailurePolicy.rules[1]"),
            ("success_indexes", gate_range, "inventory/catalog_partitions.csv", "Job spec.successPolicy.rules[0].succeededIndexes"),
            ("minimum_gate_success", int(capacity["minimum_gate_success"]), "inventory/release_capacity.csv", "Job spec.successPolicy.rules[0].succeededCount"),
        ]
        write_csv(output / "reports/policy_review.csv", ["policy_item", "actual_value", "source_file", "observable_location", "result"], [{
            "policy_item": item, "actual_value": value, "source_file": source, "observable_location": location, "result": "PASS",
        } for item, value, source, location in review])
        (output / "change_note.md").write_text(
            "# CDN目录检查变更说明\n\n维护窗口为周三。影响范围是edge-assets中的cdn-catalog-check Job及其目录映射。\n\n"
            "发布包把普通Job改成按启用目录分片的Indexed Job，停用目录不进入ConfigMap。固定镜像、逐索引回退、永久错误、基础设施中断和核心市场successPolicy均按合同与窗口容量设置。\n\n"
            "回滚材料是输入包中的starter/job.yaml。平台组负责发布包评审，维护窗值班负责现场应用、回滚准备和运行观察。\n",
            encoding="utf-8",
        )
    except Exception:
        if output.exists():
            shutil.rmtree(output)
        raise


if __name__ == "__main__":
    main()
