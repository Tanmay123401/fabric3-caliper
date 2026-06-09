#!/usr/bin/env python3

import argparse
import json
import math
import os
import pathlib
import re
import shutil

os.environ.setdefault("MPLCONFIGDIR", "/tmp/mpl-cache")
os.environ.setdefault("XDG_CACHE_HOME", "/tmp/xdg-cache")

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent
TOP_LEVEL_RESULTS_ROOT = WORKSPACE_ROOT / "paper-results" / "bottleneck-analysis"
LOG_KEYWORDS = {
    "block": re.compile(r"block|cut|create", re.IGNORECASE),
    "validation": re.compile(r"validat|vscc|mvcc", re.IGNORECASE),
    "queue": re.compile(r"queue|backlog|buffer", re.IGNORECASE),
    "timeout": re.compile(r"timeout|deadline|unavailable", re.IGNORECASE),
}


def parse_args():
    parser = argparse.ArgumentParser(description="Analyze Fabric transaction bottleneck runs.")
    parser.add_argument("--input-dir", required=True, help="Input directory created by run_bottleneck_analysis.js")
    return parser.parse_args()


def find_run_dirs(input_dir: pathlib.Path):
    manifest_path = input_dir / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        return [pathlib.Path(item["run_dir"]) for item in manifest["runs"]]
    return sorted(path for path in input_dir.iterdir() if path.is_dir() and path.name.startswith("tps_"))


def parse_percent(text):
    if pd.isna(text):
        return math.nan
    cleaned = str(text).replace("%", "").strip()
    return float(cleaned) if cleaned else math.nan


def parse_memory_usage(text):
    if pd.isna(text):
        return math.nan
    raw = str(text).split("/")[0].strip()
    match = re.match(r"([0-9.]+)([A-Za-z]+)", raw)
    if not match:
        return math.nan
    value = float(match.group(1))
    unit = match.group(2).lower()
    factors = {
        "b": 1 / (1024 ** 2),
        "kib": 1 / 1024,
        "kb": 1 / 1024,
        "mib": 1,
        "mb": 1,
        "gib": 1024,
        "gb": 1024,
    }
    return value * factors.get(unit, math.nan)


def summarize_logs(log_dir: pathlib.Path, target_tps: int):
    rows = []
    for log_file in sorted(log_dir.glob("*.log")):
        if log_file.name.endswith(".tail.log"):
            continue
        text = log_file.read_text(encoding="utf-8", errors="ignore")
        row = {
            "target_tps": target_tps,
            "container": log_file.stem,
            "line_count": text.count("\n") + (1 if text else 0),
        }
        for label, pattern in LOG_KEYWORDS.items():
            row[f"{label}_hits"] = len(pattern.findall(text))
        rows.append(row)
    return rows


def summarize_stage_timings(stage_df: pd.DataFrame, target_tps: int):
    success_df = stage_df[stage_df["status"] == "success"].copy()
    failed_df = stage_df[stage_df["status"] != "success"].copy()

    summary = {
        "target_tps": target_tps,
        "submitted": len(stage_df),
        "success": len(success_df),
        "failed": len(failed_df),
        "failure_rate_pct": float(len(failed_df) / len(stage_df) * 100.0) if len(stage_df) else 0.0,
        "throughput_tps": 0.0,
    }

    stage_columns = [
        "endorsement_delay_ms",
        "ordering_delay_ms",
        "commit_delay_ms",
        "total_latency_ms",
    ]

    if success_df.empty:
        for column in stage_columns:
            summary[f"mean_{column}"] = math.nan
            summary[f"p95_{column}"] = math.nan
        summary["bottleneck_stage_mean"] = "unknown"
        summary["bottleneck_stage_p95"] = "unknown"
        return summary

    start_ms = success_df["t1_proposal_sent_ms"].min()
    end_ms = success_df["t4_committed_ms"].max()
    elapsed_s = (end_ms - start_ms) / 1000.0
    summary["throughput_tps"] = len(success_df) / elapsed_s if elapsed_s > 0 else 0.0

    mean_candidates = {}
    p95_candidates = {}
    for column in stage_columns:
        summary[f"mean_{column}"] = float(success_df[column].mean())
        summary[f"p95_{column}"] = float(np.percentile(success_df[column], 95))
        if column != "total_latency_ms":
            mean_candidates[column] = summary[f"mean_{column}"]
            p95_candidates[column] = summary[f"p95_{column}"]

    summary["bottleneck_stage_mean"] = max(mean_candidates, key=mean_candidates.get).replace("_delay_ms", "")
    summary["bottleneck_stage_p95"] = max(p95_candidates, key=p95_candidates.get).replace("_delay_ms", "")
    return summary


def summarize_resources(resource_df: pd.DataFrame, target_tps: int):
    if resource_df.empty:
        return []

    resource_df = resource_df.copy()
    resource_df["cpu_percent_value"] = resource_df["cpu_percent"].apply(parse_percent)
    resource_df["memory_percent_value"] = resource_df["memory_percent"].apply(parse_percent)
    resource_df["memory_used_mib"] = resource_df["memory_usage"].apply(parse_memory_usage)

    rows = []
    for container, container_df in resource_df.groupby("container"):
        rows.append({
            "target_tps": target_tps,
            "container": container,
            "avg_cpu_percent": float(container_df["cpu_percent_value"].mean()),
            "max_cpu_percent": float(container_df["cpu_percent_value"].max()),
            "avg_memory_percent": float(container_df["memory_percent_value"].mean()),
            "max_memory_percent": float(container_df["memory_percent_value"].max()),
            "avg_memory_used_mib": float(container_df["memory_used_mib"].mean()),
            "max_memory_used_mib": float(container_df["memory_used_mib"].max()),
        })
    return rows


def make_stage_plot(summary_df: pd.DataFrame, output_dir: pathlib.Path):
    plt.figure(figsize=(10, 6))
    plt.plot(summary_df["target_tps"], summary_df["p95_endorsement_delay_ms"], marker="o", label="Endorsement p95")
    plt.plot(summary_df["target_tps"], summary_df["p95_ordering_delay_ms"], marker="o", label="Ordering p95")
    plt.plot(summary_df["target_tps"], summary_df["p95_commit_delay_ms"], marker="o", label="Commit p95")
    plt.xlabel("Target Load (TPS)")
    plt.ylabel("Stage Delay (ms)")
    plt.title("Fabric stage delays vs offered load")
    plt.grid(alpha=0.3)
    plt.legend()
    plt.tight_layout()
    plt.savefig(output_dir / "stage_delay_p95_vs_tps.png", dpi=200)
    plt.close()


def make_stacked_mean_plot(summary_df: pd.DataFrame, output_dir: pathlib.Path):
    plt.figure(figsize=(10, 6))
    plt.stackplot(
        summary_df["target_tps"],
        summary_df["mean_endorsement_delay_ms"],
        summary_df["mean_ordering_delay_ms"],
        summary_df["mean_commit_delay_ms"],
        labels=["Endorsement", "Ordering", "Commit"],
        alpha=0.85,
    )
    plt.xlabel("Target Load (TPS)")
    plt.ylabel("Mean Delay (ms)")
    plt.title("Mean transaction latency decomposition")
    plt.legend(loc="upper left")
    plt.tight_layout()
    plt.savefig(output_dir / "mean_latency_decomposition.png", dpi=200)
    plt.close()


def make_resource_plot(resource_summary_df: pd.DataFrame, output_dir: pathlib.Path):
    if resource_summary_df.empty:
        return

    fig, axes = plt.subplots(2, 1, figsize=(10, 10), sharex=True)
    for container, container_df in resource_summary_df.groupby("container"):
        axes[0].plot(container_df["target_tps"], container_df["avg_cpu_percent"], marker="o", label=container)
        axes[1].plot(container_df["target_tps"], container_df["avg_memory_used_mib"], marker="o", label=container)

    axes[0].set_ylabel("Avg CPU (%)")
    axes[0].set_title("Container CPU by load")
    axes[0].grid(alpha=0.3)
    axes[1].set_ylabel("Avg Memory (MiB)")
    axes[1].set_xlabel("Target Load (TPS)")
    axes[1].set_title("Container memory by load")
    axes[1].grid(alpha=0.3)
    axes[0].legend(loc="upper left")
    plt.tight_layout()
    plt.savefig(output_dir / "resource_utilization_vs_tps.png", dpi=200)
    plt.close()


def make_log_heatmap(log_summary_df: pd.DataFrame, output_dir: pathlib.Path):
    if log_summary_df.empty:
        return

    condensed = (
        log_summary_df.groupby(["container", "target_tps"], as_index=False)["timeout_hits"]
        .sum()
    )
    condensed["container_metric"] = condensed["container"] + " / timeout_hits"
    pivot = condensed.pivot(index="container_metric", columns="target_tps", values="timeout_hits").fillna(0)

    plt.figure(figsize=(10, 4))
    plt.imshow(pivot, aspect="auto", cmap="OrRd", interpolation="nearest")
    plt.colorbar(label="Timeout-related log hits")
    plt.yticks(range(len(pivot.index)), pivot.index)
    plt.xticks(range(len(pivot.columns)), pivot.columns)
    plt.xlabel("Target Load (TPS)")
    plt.title("Timeout/error log intensity")
    plt.tight_layout()
    plt.savefig(output_dir / "timeout_log_heatmap.png", dpi=200)
    plt.close()


def write_markdown(summary_df: pd.DataFrame, resource_summary_df: pd.DataFrame, log_summary_df: pd.DataFrame, output_dir: pathlib.Path):
    overload_rows = summary_df[summary_df["throughput_tps"] < summary_df["target_tps"] * 0.9]
    overload_row = overload_rows.iloc[0] if not overload_rows.empty else None

    lines = [
        "# Bottleneck Stage Analysis",
        "",
        "## Key findings",
    ]

    stable_rows = summary_df[summary_df["throughput_tps"] >= summary_df["target_tps"] * 0.9]
    if not stable_rows.empty:
        best_stable = stable_rows.iloc[-1]
        lines.append(
            f"- Highest stable load observed: {int(best_stable['target_tps'])} TPS with bottleneck stage `{best_stable['bottleneck_stage_p95']}` at p95."
        )

    if overload_row is not None:
        lines.append(
            f"- First overload point observed: {int(overload_row['target_tps'])} TPS, where achieved throughput was {overload_row['throughput_tps']:.1f} TPS and the dominant p95 stage was `{overload_row['bottleneck_stage_p95']}`."
        )
    else:
        lines.append("- No overload point was observed in the tested load range.")

    peak_commit = summary_df.sort_values("p95_commit_delay_ms", ascending=False).iloc[0]
    lines.append(
        f"- Largest commit-stage delay: {int(peak_commit['target_tps'])} TPS with p95 commit delay {peak_commit['p95_commit_delay_ms']:.1f} ms."
    )

    if not resource_summary_df.empty:
        hottest = resource_summary_df.sort_values("avg_cpu_percent", ascending=False).iloc[0]
        lines.append(
            f"- Highest average CPU load was on `{hottest['container']}` at {int(hottest['target_tps'])} TPS with {hottest['avg_cpu_percent']:.1f}% CPU."
        )

    if not log_summary_df.empty:
        timeout_heavy = log_summary_df.sort_values("timeout_hits", ascending=False).iloc[0]
        lines.append(
            f"- Most timeout-related log activity came from `{timeout_heavy['container']}` at {int(timeout_heavy['target_tps'])} TPS with {int(timeout_heavy['timeout_hits'])} matching log lines."
        )

    lines.extend([
        "",
        "## Artifacts",
        "- `stage_summary.csv`: stage-delay summary per load point.",
        "- `resource_summary.csv`: averaged Docker CPU/memory metrics per container and load point.",
        "- `log_summary.csv`: keyword-hit summary from peer/orderer logs.",
        "- `stage_delay_p95_vs_tps.png`: p95 endorsement, ordering, and commit delays vs load.",
        "- `mean_latency_decomposition.png`: stacked mean latency decomposition by stage.",
        "- `resource_utilization_vs_tps.png`: average CPU and memory by container and load.",
        "- `timeout_log_heatmap.png`: timeout-related log intensity across containers and load points.",
    ])

    (output_dir / "analysis.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def copy_top_level_outputs(input_dir: pathlib.Path):
    destination = TOP_LEVEL_RESULTS_ROOT / input_dir.name
    destination.mkdir(parents=True, exist_ok=True)
    for item in input_dir.iterdir():
        if item.is_file() and item.suffix.lower() in {".csv", ".png", ".md", ".json"}:
            shutil.copy2(item, destination / item.name)


def main():
    args = parse_args()
    input_dir = pathlib.Path(args.input_dir).resolve()
    run_dirs = find_run_dirs(input_dir)

    stage_rows = []
    resource_rows = []
    log_rows = []

    for run_dir in run_dirs:
        metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
        target_tps = metadata["target_tps"]

        stage_df = pd.read_csv(run_dir / "stage_timings.csv")
        stage_rows.append(summarize_stage_timings(stage_df, target_tps))

        resource_path = run_dir / "resource_metrics.csv"
        if resource_path.exists():
            resource_df = pd.read_csv(resource_path)
            resource_rows.extend(summarize_resources(resource_df, target_tps))

        log_dir = run_dir / "container-logs"
        if log_dir.exists():
            log_rows.extend(summarize_logs(log_dir, target_tps))

    stage_summary_df = pd.DataFrame(stage_rows).sort_values("target_tps").reset_index(drop=True)
    resource_summary_df = pd.DataFrame(resource_rows).sort_values(["container", "target_tps"]).reset_index(drop=True) if resource_rows else pd.DataFrame()
    log_summary_df = pd.DataFrame(log_rows).sort_values(["container", "target_tps"]).reset_index(drop=True) if log_rows else pd.DataFrame()

    stage_summary_df.to_csv(input_dir / "stage_summary.csv", index=False)
    if not resource_summary_df.empty:
        resource_summary_df.to_csv(input_dir / "resource_summary.csv", index=False)
    if not log_summary_df.empty:
        log_summary_df.to_csv(input_dir / "log_summary.csv", index=False)

    make_stage_plot(stage_summary_df, input_dir)
    make_stacked_mean_plot(stage_summary_df, input_dir)
    make_resource_plot(resource_summary_df, input_dir)
    make_log_heatmap(log_summary_df, input_dir)
    write_markdown(stage_summary_df, resource_summary_df, log_summary_df, input_dir)
    copy_top_level_outputs(input_dir)

    print(f"Analysis written to {input_dir}")


if __name__ == "__main__":
    main()
