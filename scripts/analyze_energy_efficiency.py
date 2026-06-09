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
DEFAULT_BENCHMARK_SUMMARY = WORKSPACE_ROOT / "paper-results" / "queueing-analysis" / "benchmark_summary.csv"
DEFAULT_RESOURCE_SUMMARY = WORKSPACE_ROOT / "paper-results" / "bottleneck-analysis" / "paper-initial" / "resource_summary.csv"
DEFAULT_OUTPUT = WORKSPACE_ROOT / "paper-results" / "energy-efficiency-analysis"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Compute energy/resource efficiency metrics from existing Fabric benchmark results."
    )
    parser.add_argument(
        "--benchmark-summary",
        default=str(DEFAULT_BENCHMARK_SUMMARY),
        help="CSV containing target_tps, throughput_tps, latency, and success_tx columns.",
    )
    parser.add_argument(
        "--resource-summary",
        default=str(DEFAULT_RESOURCE_SUMMARY),
        help="CSV containing per-container avg CPU and memory usage by target_tps.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT),
        help="Directory where energy efficiency artifacts are written.",
    )
    return parser.parse_args()


def safe_divide(numerator, denominator):
    if pd.isna(denominator) or denominator == 0:
        return math.nan
    return numerator / denominator


def normalize_benchmark_summary(df):
    rename_map = {}
    if "throughput" in df.columns and "throughput_tps" not in df.columns:
        rename_map["throughput"] = "throughput_tps"
    if "mean_latency_ms" in df.columns and "mean_total_latency_ms" not in df.columns:
        rename_map["mean_latency_ms"] = "mean_total_latency_ms"
    if "p95_latency_ms" in df.columns and "p95_total_latency_ms" not in df.columns:
        rename_map["p95_latency_ms"] = "p95_total_latency_ms"
    if "success" in df.columns and "success_tx" not in df.columns:
        rename_map["success"] = "success_tx"
    normalized = df.rename(columns=rename_map).copy()

    required = ["target_tps", "throughput_tps", "mean_total_latency_ms", "p95_total_latency_ms", "success_tx"]
    missing = [column for column in required if column not in normalized.columns]
    if missing:
        raise ValueError(f"Benchmark summary is missing required columns: {', '.join(missing)}")
    return normalized[required]


def compute_energy_efficiency(benchmark_df, resource_df):
    resource_total = (
        resource_df.groupby("target_tps", as_index=False)
        .agg(
            cpu_percent=("avg_cpu_percent", "sum"),
            memory_mib=("avg_memory_used_mib", "sum"),
            avg_memory_percent=("avg_memory_percent", "sum"),
        )
    )
    df = benchmark_df.merge(resource_total, on="target_tps", how="inner")
    df["cpu_efficiency"] = df.apply(
        lambda row: safe_divide(row["throughput_tps"], row["cpu_percent"]),
        axis=1,
    )
    df["memory_efficiency"] = df.apply(
        lambda row: safe_divide(row["throughput_tps"], row["memory_mib"]),
        axis=1,
    )
    df["resource_score"] = df["cpu_percent"] + (df["memory_mib"] / 100.0)
    df["composite_efficiency"] = df.apply(
        lambda row: safe_divide(row["throughput_tps"], row["resource_score"]),
        axis=1,
    )
    df["cpu_cost_per_tx"] = df.apply(
        lambda row: safe_divide(row["cpu_percent"], row["throughput_tps"]),
        axis=1,
    )
    df["memory_cost_per_tx"] = df.apply(
        lambda row: safe_divide(row["memory_mib"], row["throughput_tps"]),
        axis=1,
    )

    ordered = [
        "target_tps",
        "throughput_tps",
        "mean_total_latency_ms",
        "p95_total_latency_ms",
        "success_tx",
        "cpu_percent",
        "memory_mib",
        "avg_memory_percent",
        "cpu_efficiency",
        "memory_efficiency",
        "resource_score",
        "composite_efficiency",
        "cpu_cost_per_tx",
        "memory_cost_per_tx",
    ]
    return df[ordered].sort_values("target_tps").reset_index(drop=True)


def plot_line(df, x_column, y_column, title, ylabel, output_path):
    plt.figure(figsize=(10, 6))
    plt.plot(df[x_column], df[y_column], marker="o", linewidth=2)
    plt.xlabel("Offered Load (Target TPS)")
    plt.ylabel(ylabel)
    plt.title(title)
    plt.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(output_path, dpi=200)
    plt.close()


def plot_composite_vs_latency(df, output_path):
    plt.figure(figsize=(10, 6))
    scatter = plt.scatter(
        df["p95_total_latency_ms"],
        df["composite_efficiency"],
        c=df["target_tps"],
        s=100,
        cmap="viridis",
    )
    for _, row in df.iterrows():
        plt.annotate(
            f"{int(row['target_tps'])}",
            (row["p95_total_latency_ms"], row["composite_efficiency"]),
            textcoords="offset points",
            xytext=(6, 6),
            fontsize=9,
        )
    plt.colorbar(scatter, label="Target TPS")
    plt.xlabel("P95 Total Latency (ms)")
    plt.ylabel("Composite Efficiency")
    plt.title("Composite Efficiency vs Latency")
    plt.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(output_path, dpi=200)
    plt.close()


def generate_plots(df, output_dir):
    plot_line(
        df,
        "target_tps",
        "cpu_efficiency",
        "CPU Energy Efficiency vs Offered Load",
        "Throughput per CPU %",
        output_dir / "cpu_energy_efficiency_vs_offered_load.png",
    )
    plot_line(
        df,
        "target_tps",
        "memory_efficiency",
        "Memory Energy Efficiency vs Offered Load",
        "Throughput per MiB",
        output_dir / "memory_energy_efficiency_vs_offered_load.png",
    )
    plot_line(
        df,
        "target_tps",
        "composite_efficiency",
        "Composite Efficiency vs Offered Load",
        "Throughput per Resource Score",
        output_dir / "composite_efficiency_vs_offered_load.png",
    )
    plot_line(
        df,
        "target_tps",
        "cpu_cost_per_tx",
        "CPU Cost per Transaction vs Offered Load",
        "CPU % per TPS",
        output_dir / "cpu_cost_per_transaction_vs_offered_load.png",
    )
    plot_line(
        df,
        "target_tps",
        "memory_cost_per_tx",
        "Memory Cost per Transaction vs Offered Load",
        "MiB per TPS",
        output_dir / "memory_cost_per_transaction_vs_offered_load.png",
    )
    plot_composite_vs_latency(df, output_dir / "composite_efficiency_vs_latency.png")


def percent_change(old, new):
    if pd.isna(old) or old == 0:
        return math.nan
    return ((new - old) / old) * 100.0


def describe_resource_trend(df):
    first = df.sort_values("target_tps").iloc[0]
    last = df.sort_values("target_tps").iloc[-1]
    cpu_delta = percent_change(first["cpu_percent"], last["cpu_percent"])
    mem_delta = percent_change(first["memory_mib"], last["memory_mib"])
    return (
        f"CPU rises from {first['cpu_percent']:.2f}% to {last['cpu_percent']:.2f}% "
        f"({cpu_delta:.1f}% change), while memory rises from {first['memory_mib']:.1f} MiB "
        f"to {last['memory_mib']:.1f} MiB ({mem_delta:.1f}% change)."
    )


def write_markdown(df, output_dir, benchmark_path, resource_path):
    peak = df.sort_values("composite_efficiency", ascending=False).iloc[0]
    least = df.sort_values("composite_efficiency", ascending=True).iloc[0]
    max_load = df.sort_values("target_tps").iloc[-1]
    efficiency_drop = percent_change(peak["composite_efficiency"], max_load["composite_efficiency"])
    latency_at_peak = peak["p95_total_latency_ms"]
    latency_at_max = max_load["p95_total_latency_ms"]

    lines = [
        "# Energy Efficiency Analysis",
        "",
        "## Input Data",
        f"- Benchmark summary: `{benchmark_path}`.",
        f"- Resource summary: `{resource_path}`.",
        "- Values are derived from existing benchmark results; no synthetic measurements are used.",
        "",
        "## Metrics",
        "- CPU Energy Efficiency: `throughput_tps / cpu_percent`.",
        "- Memory Energy Efficiency: `throughput_tps / memory_mib`.",
        "- Composite Resource Score: `cpu_percent + (memory_mib / 100)`.",
        "- Composite Efficiency: `throughput_tps / resource_score`.",
        "- CPU Cost per Transaction: `cpu_percent / throughput_tps`.",
        "- Memory Cost per Transaction: `memory_mib / throughput_tps`.",
        "",
        "## Key Findings",
        f"- Peak energy-efficient operating point: {int(peak['target_tps'])} TPS with composite efficiency {peak['composite_efficiency']:.4f}.",
        f"- Least efficient operating point: {int(least['target_tps'])} TPS with composite efficiency {least['composite_efficiency']:.4f}.",
        f"- Efficiency drop after peak/saturation: {efficiency_drop:.2f}% by {int(max_load['target_tps'])} TPS.",
        f"- Resource utilization trend: {describe_resource_trend(df)}",
        "",
        "## Interpretation",
        "- Efficiency improves initially because fixed network and container overhead is amortized across more successful transactions as offered load increases.",
        "- Efficiency decreases near saturation when extra CPU and memory pressure no longer produce proportional throughput gains.",
        "- Latency, throughput, and efficiency are coupled: rising throughput can improve efficiency until latency spikes indicate queueing and scheduling pressure, after which resource cost per transaction grows.",
        f"- At the peak efficiency point ({int(peak['target_tps'])} TPS), p95 latency is {latency_at_peak:.1f} ms.",
        f"- At the highest offered load ({int(max_load['target_tps'])} TPS), p95 latency is {latency_at_max:.1f} ms.",
        "",
        "## Bottleneck Observations",
        "- A falling composite-efficiency curve alongside rising p95 latency suggests resource saturation rather than useful scaling.",
        "- CPU pressure is weighted directly in the composite score; memory pressure contributes as `memory_mib / 100` to keep units comparable for this analysis.",
        "",
        "## Artifacts",
        "- `energy_efficiency_analysis.csv`.",
        "- `cpu_energy_efficiency_vs_offered_load.png`.",
        "- `memory_energy_efficiency_vs_offered_load.png`.",
        "- `composite_efficiency_vs_offered_load.png`.",
        "- `cpu_cost_per_transaction_vs_offered_load.png`.",
        "- `memory_cost_per_transaction_vs_offered_load.png`.",
        "- `composite_efficiency_vs_latency.png`.",
        "",
        "## Figures",
        "![CPU Energy Efficiency](cpu_energy_efficiency_vs_offered_load.png)",
        "![Memory Energy Efficiency](memory_energy_efficiency_vs_offered_load.png)",
        "![Composite Efficiency](composite_efficiency_vs_offered_load.png)",
        "![CPU Cost per Transaction](cpu_cost_per_transaction_vs_offered_load.png)",
        "![Memory Cost per Transaction](memory_cost_per_transaction_vs_offered_load.png)",
        "![Composite Efficiency vs Latency](composite_efficiency_vs_latency.png)",
    ]
    (output_dir / "energy_efficiency_analysis.md").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def main():
    args = parse_args()
    output_dir = pathlib.Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    benchmark_path = pathlib.Path(args.benchmark_summary).resolve()
    resource_path = pathlib.Path(args.resource_summary).resolve()
    benchmark_df = normalize_benchmark_summary(pd.read_csv(benchmark_path))
    resource_df = pd.read_csv(resource_path)

    analysis_df = compute_energy_efficiency(benchmark_df, resource_df)
    analysis_df.to_csv(output_dir / "energy_efficiency_analysis.csv", index=False)
    generate_plots(analysis_df, output_dir)
    write_markdown(analysis_df, output_dir, benchmark_path, resource_path)

    print(f"Energy Efficiency Analysis written to {output_dir}")


if __name__ == "__main__":
    main()
