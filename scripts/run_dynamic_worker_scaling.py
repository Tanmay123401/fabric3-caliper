#!/usr/bin/env python3

import argparse
import datetime as dt
import json
import math
import os
import pathlib
import re
import subprocess
import sys
import textwrap
import threading
import time

os.environ.setdefault("MPLCONFIGDIR", "/tmp/mpl-cache")
os.environ.setdefault("XDG_CACHE_HOME", "/tmp/xdg-cache")

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent
OUTPUT_DIR = WORKSPACE_ROOT / "paper-results" / "dynamic-worker-scaling"
TXINFO_PATTERN = re.compile(r"(\{\"status\":.*\})")
MONITORED_CONTAINERS = [
    "peer0.org1.example.com",
    "peer0.org2.example.com",
    "orderer.example.com",
]


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run Dynamic Worker Scaling for Fabric 3 + Caliper."
    )
    parser.add_argument("--workers", nargs="+", type=int, default=[1, 5, 10, 20, 40])
    parser.add_argument("--target-tps", type=int, default=200)
    parser.add_argument("--tx-number", type=int, default=1000)
    parser.add_argument("--payload-kb", type=int, default=1)
    parser.add_argument("--network-config", default="network.yaml")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR))
    parser.add_argument("--stats-interval", type=float, default=1.0)
    return parser.parse_args()


def ensure_dir(path):
    path.mkdir(parents=True, exist_ok=True)


def render_benchmark_yaml(args, worker_count):
    return textwrap.dedent(
        f"""\
        name: Dynamic-Worker-Scaling

        monitors:
          transaction:
            - module: logging
              options:
                loggerModuleName: txinfo
                messageLevel: info

        test:
          workers:
            number: {worker_count}
          rounds:
            - label: createAsset_workers_{worker_count}
              description: Dynamic worker scaling at {worker_count} workers
              txNumber: {args.tx_number}
              rateControl:
                type: fixed-rate
                opts:
                  tps: {args.target_tps}
              workload:
                module: workloads/createAsset.js
                arguments:
                  payloadKB: {args.payload_kb}
        """
    )


def parse_percent(value):
    if value is None:
        return math.nan
    cleaned = str(value).replace("%", "").strip()
    return float(cleaned) if cleaned else math.nan


def parse_memory_used_mib(value):
    if value is None:
        return math.nan
    raw = str(value).split("/")[0].strip()
    match = re.match(r"([0-9.]+)([A-Za-z]+)", raw)
    if not match:
        return math.nan

    number = float(match.group(1))
    unit = match.group(2).lower()
    factors = {
        "b": 1 / (1024 ** 2),
        "kb": 1 / 1024,
        "kib": 1 / 1024,
        "mb": 1,
        "mib": 1,
        "gb": 1024,
        "gib": 1024,
    }
    return number * factors.get(unit, math.nan)


def sample_docker_stats(worker_count, rows):
    cmd = [
        "docker",
        "stats",
        "--no-stream",
        "--format",
        "{{json .}}",
        *MONITORED_CONTAINERS,
    ]
    completed = subprocess.run(
        cmd,
        cwd=WORKSPACE_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    timestamp = dt.datetime.now().isoformat()
    if completed.returncode != 0:
        rows.append(
            {
                "timestamp": timestamp,
                "worker_count": worker_count,
                "container": "docker_stats_error",
                "error": completed.stderr.strip(),
            }
        )
        return

    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        rows.append(
            {
                "timestamp": timestamp,
                "worker_count": worker_count,
                "container": payload.get("Name"),
                "container_id": payload.get("ID"),
                "cpu_percent": payload.get("CPUPerc"),
                "memory_usage": payload.get("MemUsage"),
                "memory_percent": payload.get("MemPerc"),
                "net_io": payload.get("NetIO"),
                "block_io": payload.get("BlockIO"),
                "pids": payload.get("PIDs"),
                "error": "",
            }
        )


def monitor_docker_stats(worker_count, rows, stop_event, interval):
    while not stop_event.is_set():
        sample_docker_stats(worker_count, rows)
        stop_event.wait(interval)
    sample_docker_stats(worker_count, rows)


def extract_tx_records(log_path):
    records = []
    with open(log_path, "r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            match = TXINFO_PATTERN.search(line)
            if not match:
                continue
            payload = json.loads(match.group(1))
            status = payload.get("status", {})
            create_time = status.get("time_create")
            final_time = status.get("time_final")
            tx_status = status.get("status")
            if not create_time or not final_time or not tx_status:
                continue
            records.append(
                {
                    "tx_id": status.get("id"),
                    "status": tx_status,
                    "create_time_ms": create_time,
                    "final_time_ms": final_time,
                    "latency_ms": final_time - create_time,
                    "worker_index": payload.get("workerIndex"),
                    "round_index": payload.get("roundIndex"),
                }
            )
    return pd.DataFrame.from_records(records)


def summarize_transactions(tx_df, worker_count, args):
    total = len(tx_df)
    success_df = tx_df[tx_df["status"] == "success"].copy() if total else pd.DataFrame()
    failed = total - len(success_df)
    row = {
        "worker_count": worker_count,
        "target_tps": args.target_tps,
        "tx_number": args.tx_number,
        "payload_kb": args.payload_kb,
        "submitted": total,
        "success_tx": len(success_df),
        "failed_tx": failed,
        "failure_rate_pct": float(failed / total * 100.0) if total else 0.0,
        "success_rate_pct": float(len(success_df) / total * 100.0) if total else 0.0,
        "throughput_tps": 0.0,
        "avg_latency_ms": math.nan,
        "p50_latency_ms": math.nan,
        "p95_latency_ms": math.nan,
        "p99_latency_ms": math.nan,
        "max_latency_ms": math.nan,
    }

    if success_df.empty:
        return row

    elapsed_s = (
        success_df["final_time_ms"].max() - success_df["create_time_ms"].min()
    ) / 1000.0
    row["throughput_tps"] = len(success_df) / elapsed_s if elapsed_s > 0 else 0.0
    row["avg_latency_ms"] = float(success_df["latency_ms"].mean())
    row["p50_latency_ms"] = float(np.percentile(success_df["latency_ms"], 50))
    row["p95_latency_ms"] = float(np.percentile(success_df["latency_ms"], 95))
    row["p99_latency_ms"] = float(np.percentile(success_df["latency_ms"], 99))
    row["max_latency_ms"] = float(success_df["latency_ms"].max())
    return row


def summarize_resources(raw_resource_df):
    valid_df = raw_resource_df[
        raw_resource_df["container"].isin(MONITORED_CONTAINERS)
    ].copy()
    if valid_df.empty:
        return pd.DataFrame()

    valid_df["cpu_percent_value"] = valid_df["cpu_percent"].apply(parse_percent)
    valid_df["memory_used_mib"] = valid_df["memory_usage"].apply(parse_memory_used_mib)
    valid_df["memory_percent_value"] = valid_df["memory_percent"].apply(parse_percent)
    summary = (
        valid_df.groupby(["worker_count", "container"], as_index=False)
        .agg(
            avg_cpu_percent=("cpu_percent_value", "mean"),
            max_cpu_percent=("cpu_percent_value", "max"),
            avg_memory_used_mib=("memory_used_mib", "mean"),
            max_memory_used_mib=("memory_used_mib", "max"),
            avg_memory_percent=("memory_percent_value", "mean"),
            max_memory_percent=("memory_percent_value", "max"),
            samples=("timestamp", "count"),
        )
        .sort_values(["worker_count", "container"])
    )
    return summary


def run_worker_point(args, output_dir, worker_count):
    run_dir = output_dir / f"workers_{worker_count}"
    ensure_dir(run_dir)

    benchmark_path = run_dir / "benchmark.yaml"
    report_path = run_dir / "report.html"
    log_path = run_dir / "run.log"
    resource_raw_path = run_dir / "docker_stats_raw.csv"

    benchmark_path.write_text(render_benchmark_yaml(args, worker_count), encoding="utf-8")
    (run_dir / "metadata.json").write_text(
        json.dumps(
            {
                "worker_count": worker_count,
                "target_tps": args.target_tps,
                "tx_number": args.tx_number,
                "payload_kb": args.payload_kb,
                "network_config": args.network_config,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    cmd = [
        "npx",
        "caliper",
        "launch",
        "manager",
        "--caliper-workspace",
        ".",
        "--caliper-benchconfig",
        str(benchmark_path),
        "--caliper-networkconfig",
        args.network_config,
        "--caliper-flow-only-test",
        "--caliper-report-path",
        str(report_path),
    ]

    resource_rows = []
    stop_event = threading.Event()
    monitor_thread = threading.Thread(
        target=monitor_docker_stats,
        args=(worker_count, resource_rows, stop_event, args.stats_interval),
        daemon=True,
    )

    env = {
        **os.environ,
        "MPLCONFIGDIR": str(output_dir / ".mpl-cache"),
        "XDG_CACHE_HOME": str(output_dir / ".cache"),
    }

    print(f"Running worker scaling point with {worker_count} workers...", flush=True)
    monitor_thread.start()
    with log_path.open("w", encoding="utf-8") as log_file:
        process = subprocess.run(
            cmd,
            cwd=ROOT,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
    stop_event.set()
    monitor_thread.join()

    raw_resource_df = pd.DataFrame.from_records(resource_rows)
    raw_resource_df.to_csv(resource_raw_path, index=False)

    if process.returncode != 0:
        raise RuntimeError(f"Caliper failed for {worker_count} workers. See {log_path}")

    tx_df = extract_tx_records(log_path)
    tx_df.to_csv(run_dir / "tx_records.csv", index=False)
    return {
        "run_dir": str(run_dir),
        "tx_summary": summarize_transactions(tx_df, worker_count, args),
        "resource_raw": raw_resource_df,
    }


def make_line_plot(df, x, y_columns, labels, title, ylabel, output_path):
    plt.figure(figsize=(10, 6))
    for y_column, label in zip(y_columns, labels):
        plt.plot(df[x], df[y_column], marker="o", linewidth=2, label=label)
    plt.xlabel("Caliper Worker Count")
    plt.ylabel(ylabel)
    plt.title(title)
    plt.grid(alpha=0.3)
    plt.legend(loc="best")
    plt.tight_layout()
    plt.savefig(output_path, dpi=200)
    plt.close()


def make_resource_cost_plot(cost_df, output_path):
    fig, ax1 = plt.subplots(figsize=(10, 6))
    ax1.plot(
        cost_df["worker_count"],
        cost_df["cpu_cost_per_success_tx"],
        marker="o",
        linewidth=2,
        color="#d62728",
        label="CPU cost per successful tx",
    )
    ax1.set_xlabel("Caliper Worker Count")
    ax1.set_ylabel("CPU % / Successful TX", color="#d62728")
    ax1.tick_params(axis="y", labelcolor="#d62728")
    ax1.grid(alpha=0.3)

    ax2 = ax1.twinx()
    ax2.plot(
        cost_df["worker_count"],
        cost_df["memory_cost_per_success_tx"],
        marker="s",
        linewidth=2,
        color="#2ca02c",
        label="Memory cost per successful tx",
    )
    ax2.set_ylabel("MiB / Successful TX", color="#2ca02c")
    ax2.tick_params(axis="y", labelcolor="#2ca02c")

    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="best")
    plt.title("Resource Cost per Successful Transaction vs Worker Count")
    plt.tight_layout()
    plt.savefig(output_path, dpi=200)
    plt.close()


def generate_graphs(summary_df, resource_summary_df, output_dir):
    make_line_plot(
        summary_df,
        "worker_count",
        ["throughput_tps"],
        ["Throughput"],
        "Throughput vs Worker Count",
        "Throughput (TPS)",
        output_dir / "throughput_vs_worker_count.png",
    )
    make_line_plot(
        summary_df,
        "worker_count",
        ["avg_latency_ms"],
        ["Average latency"],
        "Average Latency vs Worker Count",
        "Latency (ms)",
        output_dir / "average_latency_vs_worker_count.png",
    )
    make_line_plot(
        summary_df,
        "worker_count",
        ["p95_latency_ms", "p99_latency_ms"],
        ["p95 latency", "p99 latency"],
        "P95 and P99 Latency vs Worker Count",
        "Latency (ms)",
        output_dir / "p95_p99_latency_vs_worker_count.png",
    )
    make_line_plot(
        summary_df,
        "worker_count",
        ["success_rate_pct"],
        ["Success rate"],
        "Success Rate vs Worker Count",
        "Success Rate (%)",
        output_dir / "success_rate_vs_worker_count.png",
    )

    peer_df = resource_summary_df[
        resource_summary_df["container"].isin(
            ["peer0.org1.example.com", "peer0.org2.example.com"]
        )
    ]
    cpu_pivot = peer_df.pivot(
        index="worker_count", columns="container", values="avg_cpu_percent"
    ).reset_index()
    make_line_plot(
        cpu_pivot,
        "worker_count",
        [col for col in cpu_pivot.columns if col != "worker_count"],
        [col for col in cpu_pivot.columns if col != "worker_count"],
        "Peer CPU Usage vs Worker Count",
        "Average CPU (%)",
        output_dir / "peer_cpu_usage_vs_worker_count.png",
    )

    mem_pivot = resource_summary_df.pivot(
        index="worker_count", columns="container", values="avg_memory_used_mib"
    ).reset_index()
    make_line_plot(
        mem_pivot,
        "worker_count",
        [col for col in mem_pivot.columns if col != "worker_count"],
        [col for col in mem_pivot.columns if col != "worker_count"],
        "Memory Usage vs Worker Count",
        "Average Memory (MiB)",
        output_dir / "memory_usage_vs_worker_count.png",
    )
    make_line_plot(
        summary_df,
        "worker_count",
        ["scalability_efficiency"],
        ["Scalability efficiency"],
        "Scalability Efficiency vs Worker Count",
        "Throughput / Throughput at 1 Worker",
        output_dir / "scalability_efficiency_vs_worker_count.png",
    )

    make_resource_cost_plot(summary_df, output_dir / "resource_cost_per_successful_tx_vs_worker_count.png")
    make_resource_cost_plot(summary_df, output_dir / "resource_cost_per_tx_vs_worker_count.png")


def write_analysis(summary_df, resource_summary_df, output_dir):
    one_worker_throughput = summary_df.sort_values("worker_count").iloc[0]["throughput_tps"]
    best_throughput = summary_df.sort_values("throughput_tps", ascending=False).iloc[0]
    lowest_p95 = summary_df.sort_values("p95_latency_ms").iloc[0]
    best_success = summary_df.sort_values(["success_rate_pct", "throughput_tps"], ascending=False).iloc[0]

    efficiency_df = summary_df.copy()
    efficiency_df["throughput_gain_from_prev"] = efficiency_df["throughput_tps"].diff()
    saturation_candidates = efficiency_df[
        (efficiency_df["worker_count"] > 1)
        & (efficiency_df["throughput_gain_from_prev"].fillna(0) < one_worker_throughput * 0.05)
    ]
    saturation_text = "No hard saturation point was visible in this worker range."
    if not saturation_candidates.empty:
        row = saturation_candidates.iloc[0]
        saturation_text = (
            f"Saturation begins around {int(row['worker_count'])} workers because "
            f"throughput gain over the previous point falls below 5% of 1-worker throughput."
        )

    cpu_peak = resource_summary_df.sort_values("avg_cpu_percent", ascending=False).iloc[0]
    optimal_worker = best_throughput["worker_count"]
    if summary_df["p95_latency_ms"].notna().any():
        stable_df = summary_df[
            (summary_df["success_rate_pct"] >= 99.0)
            & (summary_df["p95_latency_ms"] <= summary_df["p95_latency_ms"].median())
        ]
        if not stable_df.empty:
            optimal_worker = stable_df.sort_values("throughput_tps", ascending=False).iloc[0]["worker_count"]

    one_worker_row = summary_df.sort_values("worker_count").iloc[0]
    client_bottleneck_text = (
        "A single worker under-generates the fixed 200 TPS workload, so client-side load generation is a bottleneck at 1 worker."
        if one_worker_row["throughput_tps"] < one_worker_row["target_tps"] * 0.9
        else "A single worker drives the workload close to target, so there is no clear 1-worker client-side generation bottleneck."
    )
    if best_throughput["throughput_tps"] < best_throughput["target_tps"] * 0.9:
        client_bottleneck_text += (
            " Even the best worker count remains below 90% of target TPS, indicating that client/network coordination limits this setup before the requested 200 TPS is reached."
        )

    lines = [
        "# Dynamic Worker Scaling Analysis",
        "",
        "## Experiment Setup",
        "- Hyperledger Fabric version: 3.1.4.",
        "- Target TPS: 200.",
        "- Transaction count per run: 1000.",
        "- Payload size: 1 KB.",
        "- Worker counts: 1, 5, 10, 20, 40.",
        "- Chaincode/network: same `basic` chaincode on `mychannel` using the existing Fabric test network.",
        "",
        "## Key Findings",
        f"- Highest throughput occurs at {int(best_throughput['worker_count'])} workers: {best_throughput['throughput_tps']:.2f} TPS.",
        f"- Lowest p95 latency occurs at {int(lowest_p95['worker_count'])} workers: {lowest_p95['p95_latency_ms']:.1f} ms.",
        f"- Best success behavior occurs at {int(best_success['worker_count'])} workers with {best_success['success_rate_pct']:.2f}% success.",
        f"- {saturation_text}",
        f"- Recommended worker count for this run: {int(optimal_worker)} workers.",
        f"- Client-side bottleneck assessment: {client_bottleneck_text}",
        f"- Highest observed average container CPU is `{cpu_peak['container']}` at {int(cpu_peak['worker_count'])} workers: {cpu_peak['avg_cpu_percent']:.1f}%.",
        "",
        "## Interpretation",
        "- Throughput scaling shows whether Caliper's client-side worker pool can drive the fixed 200 TPS workload without under-generating load.",
        "- Latency trends show the cost of increasing client-side concurrency while the offered TPS stays constant.",
        "- If throughput is low at small worker counts and approaches the target as workers increase, the load generator is the bottleneck at low worker counts.",
        "- If throughput stays near target while latency and peer CPU rise, the Fabric network is absorbing the offered load but paying more scheduling and commit overhead.",
        "",
        "## Artifacts",
        "- `worker_scaling_summary.csv`: throughput, success/failure, and latency percentiles by worker count.",
        "- `resource_summary.csv`: average and max CPU/memory by container and worker count.",
        "- `docker_stats_raw.csv`: raw Docker stats samples captured during every run.",
        "- `throughput_vs_worker_count.png`.",
        "- `average_latency_vs_worker_count.png`.",
        "- `p95_p99_latency_vs_worker_count.png`.",
        "- `success_rate_vs_worker_count.png`.",
        "- `peer_cpu_usage_vs_worker_count.png`.",
        "- `memory_usage_vs_worker_count.png`.",
        "- `scalability_efficiency_vs_worker_count.png`.",
        "- `resource_cost_per_successful_tx_vs_worker_count.png`.",
        "- `resource_cost_per_tx_vs_worker_count.png`.",
    ]
    (output_dir / "analysis.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    args = parse_args()
    output_dir = pathlib.Path(args.output_dir).resolve()
    ensure_dir(output_dir)
    ensure_dir(output_dir / ".cache")

    manifest = {
        "created_at": dt.datetime.now().isoformat(),
        "target_tps": args.target_tps,
        "tx_number": args.tx_number,
        "payload_kb": args.payload_kb,
        "runs": [],
    }
    tx_summaries = []
    raw_resource_frames = []

    for worker_count in args.workers:
        result = run_worker_point(args, output_dir, worker_count)
        manifest["runs"].append(
            {"worker_count": worker_count, "run_dir": result["run_dir"]}
        )
        tx_summaries.append(result["tx_summary"])
        raw_resource_frames.append(result["resource_raw"])

    summary_df = pd.DataFrame(tx_summaries).sort_values("worker_count").reset_index(drop=True)
    throughput_at_one_worker = summary_df.iloc[0]["throughput_tps"]
    summary_df["scalability_efficiency"] = summary_df["throughput_tps"] / throughput_at_one_worker if throughput_at_one_worker else math.nan

    raw_resource_df = pd.concat(raw_resource_frames, ignore_index=True)
    raw_resource_df.to_csv(output_dir / "docker_stats_raw.csv", index=False)
    resource_summary_df = summarize_resources(raw_resource_df)

    system_resource_df = (
        resource_summary_df.groupby("worker_count", as_index=False)
        .agg(
            system_avg_cpu_percent=("avg_cpu_percent", "sum"),
            system_avg_memory_used_mib=("avg_memory_used_mib", "sum"),
        )
    )
    summary_df = summary_df.merge(system_resource_df, on="worker_count", how="left")
    summary_df["cpu_cost_per_success_tx"] = (
        summary_df["system_avg_cpu_percent"] / summary_df["success_tx"]
    )
    summary_df["memory_cost_per_success_tx"] = (
        summary_df["system_avg_memory_used_mib"] / summary_df["success_tx"]
    )

    summary_df.to_csv(output_dir / "worker_scaling_summary.csv", index=False)
    resource_summary_df.to_csv(output_dir / "resource_summary.csv", index=False)
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    generate_graphs(summary_df, resource_summary_df, output_dir)
    write_analysis(summary_df, resource_summary_df, output_dir)
    print(f"Dynamic Worker Scaling results written to {output_dir}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
