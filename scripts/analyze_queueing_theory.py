#!/usr/bin/env python3

import argparse
import math
import os
import pathlib

os.environ.setdefault("MPLCONFIGDIR", "/tmp/mpl-cache")
os.environ.setdefault("XDG_CACHE_HOME", "/tmp/xdg-cache")

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd


ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent
DEFAULT_SOURCE = WORKSPACE_ROOT / "paper-results" / "bottleneck-analysis" / "paper-initial" / "stage_summary.csv"
DEFAULT_OUTPUT = WORKSPACE_ROOT / "paper-results" / "queueing-analysis"
THROUGHPUT_COLUMNS = ["throughput_tps"]


def parse_args():
    parser = argparse.ArgumentParser(
        description="Compute M/M/1 queueing metrics from real Fabric benchmark results."
    )
    parser.add_argument(
        "--input",
        default=str(DEFAULT_SOURCE),
        help="Benchmark summary CSV or source stage summary CSV.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT),
        help="Directory where queueing-analysis artifacts are written.",
    )
    parser.add_argument(
        "--service-rate-source",
        default=str(WORKSPACE_ROOT / "paper-results"),
        help="Root folder scanned for observed throughput values.",
    )
    return parser.parse_args()


def normalize_benchmark_summary(source_df):
    column_map = {}
    if "success_tx" not in source_df.columns and "success" in source_df.columns:
        column_map["success"] = "success_tx"
    if "mean_total_latency_ms" not in source_df.columns and "mean_latency_ms" in source_df.columns:
        column_map["mean_latency_ms"] = "mean_total_latency_ms"
    if "p95_total_latency_ms" not in source_df.columns and "p95_latency_ms" in source_df.columns:
        column_map["p95_latency_ms"] = "p95_total_latency_ms"

    normalized = source_df.rename(columns=column_map).copy()
    required = [
        "target_tps",
        "throughput_tps",
        "mean_total_latency_ms",
        "p95_total_latency_ms",
        "success_tx",
    ]
    missing = [column for column in required if column not in normalized.columns]
    if missing:
        raise ValueError(f"Input is missing required columns: {', '.join(missing)}")

    return normalized[required].sort_values("target_tps").reset_index(drop=True)


def find_max_observed_throughput(search_root):
    root = pathlib.Path(search_root)
    values = []
    for csv_path in root.rglob("*.csv"):
        try:
            df = pd.read_csv(csv_path)
        except Exception:
            continue
        for column in THROUGHPUT_COLUMNS:
            if column in df.columns:
                values.extend(pd.to_numeric(df[column], errors="coerce").dropna().tolist())
    if not values:
        raise ValueError(f"No throughput_tps values found under {root}")
    return max(values)


def compute_queueing_metrics(benchmark_df, service_rate_mu):
    rows = []
    for _, row in benchmark_df.iterrows():
        arrival_rate = float(row["throughput_tps"])
        rho = arrival_rate / service_rate_mu if service_rate_mu else math.nan

        if rho >= 1:
            avg_queue_length = math.inf
            avg_system_length = math.inf
            avg_waiting_time = math.inf
            avg_response_time = math.inf
            queue_delay_pct = math.inf
        else:
            avg_queue_length = (rho ** 2) / (1 - rho)
            avg_system_length = rho / (1 - rho)
            avg_waiting_time = avg_queue_length / arrival_rate if arrival_rate else 0.0
            avg_response_time = 1 / (service_rate_mu - arrival_rate)
            queue_delay_pct = (avg_waiting_time / avg_response_time) * 100 if avg_response_time else math.nan

        rows.append(
            {
                "target_tps": row["target_tps"],
                "arrival_rate_lambda": arrival_rate,
                "service_rate_mu": service_rate_mu,
                "utilization_rho": rho,
                "avg_queue_length": avg_queue_length,
                "avg_system_length": avg_system_length,
                "avg_waiting_time_s": avg_waiting_time,
                "avg_response_time_s": avg_response_time,
                "queue_delay_pct": queue_delay_pct,
            }
        )
    return pd.DataFrame(rows)


def plot_metric(df, y_column, title, ylabel, output_path, thresholds=None):
    plt.figure(figsize=(10, 6))
    plt.plot(df["target_tps"], df[y_column], marker="o", linewidth=2)
    if thresholds:
        for value, label, color in thresholds:
            plt.axhline(value, linestyle="--", linewidth=1.5, color=color, label=label)
        plt.legend(loc="best")
    plt.xlabel("Offered Load (Target TPS)")
    plt.ylabel(ylabel)
    plt.title(title)
    plt.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(output_path, dpi=200)
    plt.close()


def first_exceeds(df, column, threshold):
    exceeded = df[df[column] > threshold].sort_values("target_tps")
    if exceeded.empty:
        return None
    return exceeded.iloc[0]


def write_markdown(benchmark_df, queue_df, max_observed_throughput, output_dir):
    warning = first_exceeds(queue_df, "utilization_rho", 0.8)
    saturation = first_exceeds(queue_df, "utilization_rho", 0.9)
    max_queue = queue_df.sort_values("avg_queue_length", ascending=False).iloc[0]
    max_wait = queue_df.sort_values("avg_waiting_time_s", ascending=False).iloc[0]

    def threshold_text(row, threshold):
        if row is None:
            return f"Utilization did not exceed {threshold:.1f} in the analyzed workload range."
        return (
            f"Utilization first exceeds {threshold:.1f} at target {int(row['target_tps'])} TPS "
            f"(rho={row['utilization_rho']:.3f})."
        )

    lines = [
        "# Queueing Theory Analysis",
        "",
        "## Input",
        "- Source data: real Hyperledger Fabric benchmark results.",
        "- `benchmark_summary.csv` is normalized from the bottleneck stage summary.",
        f"- Maximum observed throughput across available experiment CSVs: {max_observed_throughput:.3f} TPS.",
        f"- Service rate with 5% safety margin: {queue_df['service_rate_mu'].iloc[0]:.3f} TPS.",
        "",
        "## Formulas",
        "- Arrival rate: `lambda = measured throughput_tps`.",
        "- Service rate: `mu = max_observed_throughput * 1.05`.",
        "- Utilization: `rho = lambda / mu`.",
        "- Average queue length: `Lq = rho^2 / (1 - rho)`.",
        "- Average system length: `L = rho / (1 - rho)`.",
        "- Average waiting time: `Wq = Lq / lambda`.",
        "- Average response time: `W = 1 / (mu - lambda)`.",
        "- Queue delay percentage: `queue_delay_pct = (Wq / W) * 100`.",
        "",
        "## Computed Metrics",
        f"- {threshold_text(warning, 0.8)}",
        f"- {threshold_text(saturation, 0.9)}",
        f"- Maximum queue length occurs at target {int(max_queue['target_tps'])} TPS: Lq={max_queue['avg_queue_length']:.3f}.",
        f"- Maximum waiting time occurs at target {int(max_wait['target_tps'])} TPS: Wq={max_wait['avg_waiting_time_s']:.4f} s.",
        "",
        "## Interpretation",
        "- The M/M/1 model treats the Fabric transaction pipeline as a single service station.",
        "- As utilization approaches 1.0, queue length and waiting time rise nonlinearly.",
        "- The warning threshold is rho > 0.8; saturation pressure is rho > 0.9.",
        "- Queueing delay percentage estimates how much response time is caused by waiting rather than service capacity.",
        "",
        "## Saturation Point",
    ]
    if saturation is None:
        lines.append("- No modeled saturation point above rho=0.9 was observed in the analyzed range.")
    else:
        lines.append(
            f"- Modeled saturation begins at target {int(saturation['target_tps'])} TPS, where rho={saturation['utilization_rho']:.3f}."
        )

    lines.extend(
        [
            "",
            "## Bottleneck Observations",
            f"- The queueing model predicts the heaviest backlog at target {int(max_queue['target_tps'])} TPS.",
            f"- At that point, the observed benchmark p95 total latency was {benchmark_df.loc[benchmark_df['target_tps'] == max_queue['target_tps'], 'p95_total_latency_ms'].iloc[0]:.1f} ms.",
            "- This links the empirical latency growth to utilization-driven queue buildup.",
            "",
            "## Artifacts",
            "- `benchmark_summary.csv`: normalized real benchmark input.",
            "- `queueing_analysis.csv`: M/M/1 queueing metrics.",
            "- `utilization_vs_offered_load.png`.",
            "- `queue_length_vs_offered_load.png`.",
            "- `waiting_time_vs_offered_load.png`.",
            "- `response_time_vs_offered_load.png`.",
            "- `queue_delay_percentage_vs_offered_load.png`.",
        ]
    )

    (output_dir / "queueing_analysis.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    args = parse_args()
    output_dir = pathlib.Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    source_df = pd.read_csv(args.input)
    benchmark_df = normalize_benchmark_summary(source_df)
    benchmark_df.to_csv(output_dir / "benchmark_summary.csv", index=False)

    max_observed_throughput = find_max_observed_throughput(args.service_rate_source)
    service_rate_mu = max_observed_throughput * 1.05
    queue_df = compute_queueing_metrics(benchmark_df, service_rate_mu)
    queue_df.to_csv(output_dir / "queueing_analysis.csv", index=False)

    plot_metric(
        queue_df,
        "utilization_rho",
        "Utilization vs Offered Load",
        "Utilization (rho)",
        output_dir / "utilization_vs_offered_load.png",
        thresholds=[
            (0.7, "healthy 0.7", "#2ca02c"),
            (0.8, "warning 0.8", "#ff7f0e"),
            (0.9, "saturation 0.9", "#d62728"),
        ],
    )
    plot_metric(
        queue_df,
        "avg_queue_length",
        "Queue Length vs Offered Load",
        "Average Queue Length (Lq)",
        output_dir / "queue_length_vs_offered_load.png",
    )
    plot_metric(
        queue_df,
        "avg_waiting_time_s",
        "Waiting Time vs Offered Load",
        "Average Waiting Time Wq (s)",
        output_dir / "waiting_time_vs_offered_load.png",
    )
    plot_metric(
        queue_df,
        "avg_response_time_s",
        "Response Time vs Offered Load",
        "Average Response Time W (s)",
        output_dir / "response_time_vs_offered_load.png",
    )
    plot_metric(
        queue_df,
        "queue_delay_pct",
        "Queue Delay Percentage vs Offered Load",
        "Queue Delay (%)",
        output_dir / "queue_delay_percentage_vs_offered_load.png",
    )
    write_markdown(benchmark_df, queue_df, max_observed_throughput, output_dir)

    print(f"Queueing Theory Analysis written to {output_dir}")


if __name__ == "__main__":
    main()
