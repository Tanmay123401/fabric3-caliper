#!/usr/bin/env python3

import argparse
import math
import os
import pathlib
import shutil

os.environ.setdefault("MPLCONFIGDIR", "/tmp/mpl-cache")
os.environ.setdefault("XDG_CACHE_HOME", "/tmp/xdg-cache")

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd


DEFAULT_INPUT_DIR = (
    pathlib.Path(__file__).resolve().parents[2]
    / "paper-results"
    / "bottleneck-analysis"
    / "paper-initial"
)
DEFAULT_OUTPUT_DIR = (
    pathlib.Path(__file__).resolve().parents[2]
    / "paper-results"
    / "resource-efficiency-analysis"
    / "paper-initial"
)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Compute resource efficiency metrics from existing bottleneck-analysis CSVs."
    )
    parser.add_argument(
        "--input-dir",
        default=str(DEFAULT_INPUT_DIR),
        help="Folder containing stage_summary.csv and resource_summary.csv.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Folder where Resource Efficiency Analysis outputs are written.",
    )
    return parser.parse_args()


def safe_divide(numerator, denominator):
    if denominator == 0 or pd.isna(denominator):
        return math.nan
    return numerator / denominator


def load_inputs(input_dir):
    stage_path = input_dir / "stage_summary.csv"
    resource_path = input_dir / "resource_summary.csv"

    if not stage_path.exists():
        raise FileNotFoundError(f"Missing required file: {stage_path}")
    if not resource_path.exists():
        raise FileNotFoundError(f"Missing required file: {resource_path}")

    stage_df = pd.read_csv(stage_path)
    resource_df = pd.read_csv(resource_path)
    return stage_df, resource_df


def compute_efficiency(stage_df, resource_df):
    joined = resource_df.merge(
        stage_df[["target_tps", "throughput_tps", "success"]],
        on="target_tps",
        how="left",
    ).rename(columns={"success": "success_tx"})

    joined["cpu_efficiency"] = joined.apply(
        lambda row: safe_divide(row["throughput_tps"], row["avg_cpu_percent"]),
        axis=1,
    )
    joined["memory_efficiency"] = joined.apply(
        lambda row: safe_divide(row["throughput_tps"], row["avg_memory_used_mib"]),
        axis=1,
    )
    joined["scalability_efficiency"] = joined.apply(
        lambda row: safe_divide(row["throughput_tps"], row["target_tps"]),
        axis=1,
    )
    joined["cpu_cost_per_transaction"] = joined.apply(
        lambda row: safe_divide(row["avg_cpu_percent"], row["success_tx"]),
        axis=1,
    )
    joined["memory_cost_per_transaction"] = joined.apply(
        lambda row: safe_divide(row["avg_memory_used_mib"], row["success_tx"]),
        axis=1,
    )

    system_resource = (
        resource_df.groupby("target_tps", as_index=False)
        .agg(
            avg_cpu_percent=("avg_cpu_percent", "sum"),
            avg_memory_used_mib=("avg_memory_used_mib", "sum"),
            max_cpu_percent=("max_cpu_percent", "sum"),
            max_memory_used_mib=("max_memory_used_mib", "sum"),
        )
        .assign(container="system_total")
    )

    system = system_resource.merge(
        stage_df[["target_tps", "throughput_tps", "success"]],
        on="target_tps",
        how="left",
    ).rename(columns={"success": "success_tx"})

    system["cpu_efficiency"] = system.apply(
        lambda row: safe_divide(row["throughput_tps"], row["avg_cpu_percent"]),
        axis=1,
    )
    system["memory_efficiency"] = system.apply(
        lambda row: safe_divide(row["throughput_tps"], row["avg_memory_used_mib"]),
        axis=1,
    )
    system["scalability_efficiency"] = system.apply(
        lambda row: safe_divide(row["throughput_tps"], row["target_tps"]),
        axis=1,
    )
    system["cpu_cost_per_transaction"] = system.apply(
        lambda row: safe_divide(row["avg_cpu_percent"], row["success_tx"]),
        axis=1,
    )
    system["memory_cost_per_transaction"] = system.apply(
        lambda row: safe_divide(row["avg_memory_used_mib"], row["success_tx"]),
        axis=1,
    )

    ordered_columns = [
        "target_tps",
        "container",
        "throughput_tps",
        "success_tx",
        "avg_cpu_percent",
        "avg_memory_used_mib",
        "cpu_efficiency",
        "memory_efficiency",
        "scalability_efficiency",
        "cpu_cost_per_transaction",
        "memory_cost_per_transaction",
    ]

    return joined[ordered_columns], system[ordered_columns]


def plot_metric(df, metric, ylabel, title, output_path):
    plt.figure(figsize=(10, 6))
    for container, container_df in df.groupby("container"):
        plt.plot(
            container_df["target_tps"],
            container_df[metric],
            marker="o",
            label=container,
        )
    plt.xlabel("Target Load (TPS)")
    plt.ylabel(ylabel)
    plt.title(title)
    plt.grid(alpha=0.3)
    plt.legend(loc="best")
    plt.tight_layout()
    plt.savefig(output_path, dpi=200)
    plt.close()


def plot_scalability(system_df, output_path):
    plt.figure(figsize=(10, 6))
    plt.plot(
        system_df["target_tps"],
        system_df["scalability_efficiency"],
        marker="o",
        color="#1f77b4",
        label="Scalability efficiency",
    )
    plt.axhline(1.0, color="#7f7f7f", linestyle="--", label="Ideal")
    plt.xlabel("Target Load (TPS)")
    plt.ylabel("Throughput / Target TPS")
    plt.title("Scalability efficiency vs offered load")
    plt.ylim(0, max(1.1, system_df["scalability_efficiency"].max() * 1.1))
    plt.grid(alpha=0.3)
    plt.legend(loc="best")
    plt.tight_layout()
    plt.savefig(output_path, dpi=200)
    plt.close()


def plot_cost_per_tx(system_df, output_path):
    fig, ax1 = plt.subplots(figsize=(10, 6))
    ax1.plot(
        system_df["target_tps"],
        system_df["cpu_cost_per_transaction"],
        marker="o",
        color="#d62728",
        label="CPU cost per tx",
    )
    ax1.set_xlabel("Target Load (TPS)")
    ax1.set_ylabel("CPU % / Successful TX", color="#d62728")
    ax1.tick_params(axis="y", labelcolor="#d62728")
    ax1.grid(alpha=0.3)

    ax2 = ax1.twinx()
    ax2.plot(
        system_df["target_tps"],
        system_df["memory_cost_per_transaction"],
        marker="s",
        color="#2ca02c",
        label="Memory cost per tx",
    )
    ax2.set_ylabel("MiB / Successful TX", color="#2ca02c")
    ax2.tick_params(axis="y", labelcolor="#2ca02c")

    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="best")
    plt.title("Resource cost per successful transaction")
    plt.tight_layout()
    plt.savefig(output_path, dpi=200)
    plt.close()


def write_analysis(output_dir, per_container_df, system_df, source_dir):
    best_cpu = system_df.sort_values("cpu_efficiency", ascending=False).iloc[0]
    best_memory = system_df.sort_values("memory_efficiency", ascending=False).iloc[0]
    best_scalability = system_df.sort_values("scalability_efficiency", ascending=False).iloc[0]
    lowest_cpu_cost = system_df.sort_values("cpu_cost_per_transaction").iloc[0]
    lowest_memory_cost = system_df.sort_values("memory_cost_per_transaction").iloc[0]

    hottest_container = per_container_df.sort_values(
        "cpu_cost_per_transaction", ascending=False
    ).iloc[0]

    lines = [
        "# Resource Efficiency Analysis",
        "",
        "## Source Data",
        f"- Reused existing bottleneck analysis inputs from `{source_dir}`.",
        "- No benchmark, Caliper, or Fabric rerun was performed.",
        "",
        "## Metrics",
        "- `CPU Efficiency = throughput_tps / avg_cpu_percent`.",
        "- `Memory Efficiency = throughput_tps / avg_memory_used_mib`.",
        "- `Scalability Efficiency = throughput_tps / target_tps`.",
        "- `CPU Cost per Transaction = avg_cpu_percent / success_tx`.",
        "- `Memory Cost per Transaction = avg_memory_used_mib / success_tx`.",
        "",
        "## Key Findings",
        f"- Best system CPU efficiency occurs at {int(best_cpu['target_tps'])} TPS: {best_cpu['cpu_efficiency']:.4f} TPS per CPU percent.",
        f"- Best system memory efficiency occurs at {int(best_memory['target_tps'])} TPS: {best_memory['memory_efficiency']:.4f} TPS per MiB.",
        f"- Best scalability efficiency occurs at {int(best_scalability['target_tps'])} TPS: {best_scalability['scalability_efficiency']:.4f}.",
        f"- Lowest system CPU cost per transaction occurs at {int(lowest_cpu_cost['target_tps'])} TPS: {lowest_cpu_cost['cpu_cost_per_transaction']:.6f} CPU percent per successful transaction.",
        f"- Lowest system memory cost per transaction occurs at {int(lowest_memory_cost['target_tps'])} TPS: {lowest_memory_cost['memory_cost_per_transaction']:.6f} MiB per successful transaction.",
        f"- Highest per-container CPU cost is `{hottest_container['container']}` at {int(hottest_container['target_tps'])} TPS: {hottest_container['cpu_cost_per_transaction']:.6f} CPU percent per successful transaction.",
        "",
        "## Artifacts",
        "- `stage_summary.csv`: copied source stage summary used for throughput and success counts.",
        "- `resource_summary.csv`: copied source resource summary used for CPU and memory.",
        "- `resource_efficiency_by_container.csv`: per-container efficiency and cost metrics.",
        "- `resource_efficiency_system.csv`: system-total efficiency and cost metrics.",
        "- `cpu_efficiency_vs_tps.png`: CPU efficiency across load levels.",
        "- `memory_efficiency_vs_tps.png`: memory efficiency across load levels.",
        "- `scalability_efficiency_vs_tps.png`: achieved throughput relative to target load.",
        "- `resource_cost_per_tx.png`: system CPU and memory cost per successful transaction.",
    ]

    (output_dir / "analysis.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def remove_embedded_efficiency_section(bottleneck_dir):
    analysis_path = bottleneck_dir / "analysis.md"
    if not analysis_path.exists():
        return

    existing = analysis_path.read_text(encoding="utf-8")
    marker = "\n## Resource Efficiency Analysis"
    if marker in existing:
        analysis_path.write_text(existing.split(marker)[0].rstrip() + "\n", encoding="utf-8")


def main():
    args = parse_args()
    input_dir = pathlib.Path(args.input_dir).resolve()
    output_dir = pathlib.Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    stage_df, resource_df = load_inputs(input_dir)
    per_container_df, system_df = compute_efficiency(stage_df, resource_df)

    combined_plot_df = pd.concat([per_container_df, system_df], ignore_index=True)
    shutil.copy2(input_dir / "stage_summary.csv", output_dir / "stage_summary.csv")
    shutil.copy2(input_dir / "resource_summary.csv", output_dir / "resource_summary.csv")
    per_container_df.to_csv(output_dir / "resource_efficiency_by_container.csv", index=False)
    system_df.to_csv(output_dir / "resource_efficiency_system.csv", index=False)

    plot_metric(
        combined_plot_df,
        "cpu_efficiency",
        "TPS per CPU %",
        "CPU efficiency vs offered load",
        output_dir / "cpu_efficiency_vs_tps.png",
    )
    plot_metric(
        combined_plot_df,
        "memory_efficiency",
        "TPS per MiB",
        "Memory efficiency vs offered load",
        output_dir / "memory_efficiency_vs_tps.png",
    )
    plot_scalability(system_df, output_dir / "scalability_efficiency_vs_tps.png")
    plot_cost_per_tx(system_df, output_dir / "resource_cost_per_tx.png")
    write_analysis(output_dir, per_container_df, system_df, input_dir)
    remove_embedded_efficiency_section(input_dir)

    print(f"Resource Efficiency Analysis written to {output_dir}")


if __name__ == "__main__":
    main()
