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


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run adaptive Dynamic Worker Scaling for Fabric 3 + Caliper."
    )
    parser.add_argument("--initial-workers", type=int, default=5)
    parser.add_argument("--min-workers", type=int, default=1)
    parser.add_argument("--max-workers", type=int, default=50)
    parser.add_argument("--worker-step", type=int, default=5)
    parser.add_argument("--target-tps", type=int, default=250)
    parser.add_argument("--payload-kb", type=int, default=1)
    parser.add_argument("--control-interval", type=int, default=10)
    parser.add_argument("--intervals", type=int, default=5)
    parser.add_argument("--network-config", default="network.yaml")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR))
    return parser.parse_args()


def ensure_dir(path):
    path.mkdir(parents=True, exist_ok=True)


def render_benchmark_yaml(args, worker_count, interval_index):
    return textwrap.dedent(
        f"""\
        name: Adaptive-Dynamic-Worker-Scaling

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
            - label: adaptive_interval_{interval_index}_workers_{worker_count}
              description: Adaptive worker scaling interval {interval_index} with {worker_count} workers
              txDuration: {args.control_interval}
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


def summarize_interval(tx_df):
    total = len(tx_df)
    success_df = tx_df[tx_df["status"] == "success"].copy() if total else pd.DataFrame()
    success = len(success_df)
    failed = total - success
    if success_df.empty:
        return {
            "submitted": total,
            "success_tx": success,
            "failed_tx": failed,
            "throughput_tps": 0.0,
            "avg_latency_ms": math.nan,
            "p95_latency_ms": math.nan,
            "p99_latency_ms": math.nan,
            "success_rate": float(success / total * 100.0) if total else 0.0,
        }

    elapsed_s = (
        success_df["final_time_ms"].max() - success_df["create_time_ms"].min()
    ) / 1000.0
    return {
        "submitted": total,
        "success_tx": success,
        "failed_tx": failed,
        "throughput_tps": success / elapsed_s if elapsed_s > 0 else 0.0,
        "avg_latency_ms": float(success_df["latency_ms"].mean()),
        "p95_latency_ms": float(np.percentile(success_df["latency_ms"], 95)),
        "p99_latency_ms": float(np.percentile(success_df["latency_ms"], 99)),
        "success_rate": float(success / total * 100.0),
    }


def apply_scaling_policy(args, worker_count, metrics):
    p95 = metrics["p95_latency_ms"]
    success_rate = metrics["success_rate"]

    if not math.isnan(p95) and p95 < 150 and success_rate > 99:
        next_workers = min(args.max_workers, worker_count + args.worker_step)
        decision = "scale_up" if next_workers != worker_count else "hold_max_workers"
        reason = "p95_latency < 150 ms and success_rate > 99%"
    elif (not math.isnan(p95) and p95 > 500) or success_rate < 95:
        next_workers = max(args.min_workers, worker_count - args.worker_step)
        decision = "scale_down" if next_workers != worker_count else "hold_min_workers"
        reason = "p95_latency > 500 ms or success_rate < 95%"
    else:
        next_workers = worker_count
        decision = "hold"
        reason = "latency and success-rate thresholds did not trigger scaling"

    return decision, reason, next_workers


def run_interval(args, output_dir, interval_index, worker_count):
    run_dir = output_dir / f"adaptive_interval_{interval_index:02d}_workers_{worker_count}"
    ensure_dir(run_dir)

    benchmark_path = run_dir / "benchmark.yaml"
    report_path = run_dir / "report.html"
    log_path = run_dir / "run.log"
    tx_records_path = run_dir / "tx_records.csv"

    benchmark_path.write_text(
        render_benchmark_yaml(args, worker_count, interval_index),
        encoding="utf-8",
    )
    (run_dir / "metadata.json").write_text(
        json.dumps(
            {
                "interval_index": interval_index,
                "worker_count": worker_count,
                "target_tps": args.target_tps,
                "payload_kb": args.payload_kb,
                "control_interval_seconds": args.control_interval,
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
    env = {
        **os.environ,
        "MPLCONFIGDIR": str(output_dir / ".mpl-cache"),
        "XDG_CACHE_HOME": str(output_dir / ".cache"),
    }

    started_at = dt.datetime.now()
    print(
        f"Adaptive interval {interval_index}/{args.intervals}: {worker_count} workers",
        flush=True,
    )
    with log_path.open("w", encoding="utf-8") as log_file:
        process = subprocess.run(
            cmd,
            cwd=ROOT,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
    finished_at = dt.datetime.now()

    if process.returncode != 0:
        raise RuntimeError(f"Caliper failed in interval {interval_index}. See {log_path}")

    tx_df = extract_tx_records(log_path)
    tx_df.to_csv(tx_records_path, index=False)
    metrics = summarize_interval(tx_df)
    decision, reason, next_workers = apply_scaling_policy(args, worker_count, metrics)

    return {
        "timestamp": started_at.isoformat(),
        "finished_timestamp": finished_at.isoformat(),
        "interval_index": interval_index,
        "worker_count": worker_count,
        "next_worker_count": next_workers,
        "target_tps": args.target_tps,
        "submitted": metrics["submitted"],
        "success_tx": metrics["success_tx"],
        "failed_tx": metrics["failed_tx"],
        "throughput_tps": metrics["throughput_tps"],
        "avg_latency_ms": metrics["avg_latency_ms"],
        "p95_latency_ms": metrics["p95_latency_ms"],
        "p99_latency_ms": metrics["p99_latency_ms"],
        "success_rate": metrics["success_rate"],
        "scaling_decision": decision,
        "decision_reason": reason,
        "run_dir": str(run_dir),
    }


def make_line_plot(df, y_column, title, ylabel, output_path):
    plt.figure(figsize=(10, 6))
    plt.plot(df["interval_index"], df[y_column], marker="o", linewidth=2)
    plt.xlabel("Control Interval")
    plt.ylabel(ylabel)
    plt.title(title)
    plt.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(output_path, dpi=200)
    plt.close()


def generate_graphs(log_df, output_dir):
    make_line_plot(
        log_df,
        "worker_count",
        "Worker Count vs Time",
        "Worker Count",
        output_dir / "worker_count_vs_time.png",
    )
    make_line_plot(
        log_df,
        "throughput_tps",
        "Throughput vs Time",
        "Throughput (TPS)",
        output_dir / "throughput_vs_time.png",
    )
    make_line_plot(
        log_df,
        "p95_latency_ms",
        "P95 Latency vs Time",
        "P95 Latency (ms)",
        output_dir / "p95_latency_vs_time.png",
    )

    plt.figure(figsize=(10, 6))
    plt.scatter(log_df["worker_count"], log_df["throughput_tps"], s=90)
    plt.plot(log_df["worker_count"], log_df["throughput_tps"], alpha=0.45)
    plt.xlabel("Worker Count")
    plt.ylabel("Throughput (TPS)")
    plt.title("Worker Count vs Throughput")
    plt.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(output_dir / "worker_count_vs_throughput.png", dpi=200)
    plt.close()

    plt.figure(figsize=(10, 6))
    plt.scatter(log_df["worker_count"], log_df["p95_latency_ms"], s=90)
    plt.plot(log_df["worker_count"], log_df["p95_latency_ms"], alpha=0.45)
    plt.xlabel("Worker Count")
    plt.ylabel("P95 Latency (ms)")
    plt.title("Worker Count vs P95 Latency")
    plt.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(output_dir / "worker_count_vs_p95_latency.png", dpi=200)
    plt.close()


def write_analysis(args, log_df, output_dir):
    scale_decisions = log_df["scaling_decision"].value_counts().to_dict()
    max_sustainable = log_df[
        (log_df["p95_latency_ms"] <= 500) & (log_df["success_rate"] >= 95)
    ]
    max_sustainable_workers = (
        int(max_sustainable["worker_count"].max()) if not max_sustainable.empty else None
    )
    best_throughput = log_df.sort_values("throughput_tps", ascending=False).iloc[0]
    baseline = log_df.iloc[0]
    improvement = (
        (best_throughput["throughput_tps"] - baseline["throughput_tps"])
        / baseline["throughput_tps"]
        * 100.0
        if baseline["throughput_tps"] > 0
        else math.nan
    )
    first_p95 = baseline["p95_latency_ms"]
    final_p95 = log_df.iloc[-1]["p95_latency_ms"]
    spike_text = (
        f"Adaptive scaling reduced p95 latency from {first_p95:.1f} ms to {final_p95:.1f} ms."
        if final_p95 < first_p95
        else f"Adaptive scaling did not reduce p95 latency in this run; p95 moved from {first_p95:.1f} ms to {final_p95:.1f} ms."
    )

    lines = [
        "# Adaptive Dynamic Worker Scaling Analysis",
        "",
        "## Configuration",
        f"- Initial workers: {args.initial_workers}.",
        f"- Worker range: {args.min_workers} to {args.max_workers}.",
        f"- Worker step: {args.worker_step}.",
        f"- Target TPS: {args.target_tps}.",
        f"- Payload size: {args.payload_kb} KB.",
        f"- Control interval: {args.control_interval} seconds.",
        f"- Control intervals executed: {args.intervals}.",
        "",
        "## Scaling Decisions",
    ]
    for decision, count in sorted(scale_decisions.items()):
        lines.append(f"- `{decision}`: {count} interval(s).")

    lines.extend(
        [
            "",
            "## Worker Count Evolution",
            "- "
            + " -> ".join(str(int(value)) for value in log_df["worker_count"].tolist()),
            "",
            "## Key Findings",
            f"- Maximum sustainable worker count under policy thresholds: {max_sustainable_workers if max_sustainable_workers is not None else 'none observed'}.",
            f"- Best throughput observed at interval {int(best_throughput['interval_index'])} with {int(best_throughput['worker_count'])} workers: {best_throughput['throughput_tps']:.2f} TPS.",
            f"- Throughput improvement over the initial fixed-worker window: {improvement:.2f}%.",
            f"- {spike_text}",
            "",
            "## Interpretation",
            "- The policy scales down when p95 latency crosses 500 ms or success rate falls below 95%.",
            "- The policy scales up only when p95 latency is below 150 ms and success rate is above 99%.",
            "- If the worker count quickly falls to the minimum and remains there, the workload is already above the stable latency target for this Fabric setup.",
            "",
            "## Artifacts",
            "- `dynamic_scaling_log.csv`: one row per control interval.",
            "- `worker_count_vs_time.png`.",
            "- `throughput_vs_time.png`.",
            "- `p95_latency_vs_time.png`.",
            "- `worker_count_vs_throughput.png`.",
            "- `worker_count_vs_p95_latency.png`.",
        ]
    )
    (output_dir / "adaptive_scaling_analysis.md").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )

    main_analysis_path = output_dir / "analysis.md"
    if main_analysis_path.exists():
        existing = main_analysis_path.read_text(encoding="utf-8")
        marker = "\n# Adaptive Dynamic Worker Scaling Analysis"
        if marker in existing:
            existing = existing.split(marker)[0].rstrip()
        main_analysis_path.write_text(
            existing.rstrip() + "\n\n" + "\n".join(lines) + "\n",
            encoding="utf-8",
        )
    else:
        main_analysis_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    args = parse_args()
    output_dir = pathlib.Path(args.output_dir).resolve()
    ensure_dir(output_dir)
    ensure_dir(output_dir / ".cache")

    manifest = {
        "created_at": dt.datetime.now().isoformat(),
        "initial_workers": args.initial_workers,
        "min_workers": args.min_workers,
        "max_workers": args.max_workers,
        "worker_step": args.worker_step,
        "target_tps": args.target_tps,
        "payload_kb": args.payload_kb,
        "control_interval": args.control_interval,
        "intervals": args.intervals,
    }
    (output_dir / "adaptive_scaling_manifest.json").write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )

    rows = []
    worker_count = args.initial_workers
    for interval_index in range(1, args.intervals + 1):
        row = run_interval(args, output_dir, interval_index, worker_count)
        rows.append(row)
        worker_count = row["next_worker_count"]

    log_df = pd.DataFrame(rows)
    log_df.to_csv(output_dir / "dynamic_scaling_log.csv", index=False)
    generate_graphs(log_df, output_dir)
    write_analysis(args, log_df, output_dir)

    print(f"Adaptive Dynamic Worker Scaling results written to {output_dir}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
