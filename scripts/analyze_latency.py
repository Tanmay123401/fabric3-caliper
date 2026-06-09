#!/usr/bin/env python3

import argparse
import json
import math
import os
import pathlib
import re

os.environ.setdefault("MPLCONFIGDIR", "/tmp/mpl-cache")
os.environ.setdefault("XDG_CACHE_HOME", "/tmp/xdg-cache")

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


ROOT = pathlib.Path(__file__).resolve().parents[1]
TXINFO_PATTERN = re.compile(r"(\{\"status\":.*\})")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Analyze Caliper per-transaction latency logs."
    )
    parser.add_argument(
        "--input-dir",
        required=True,
        help="Sweep directory created by run_latency_sweep.py.",
    )
    return parser.parse_args()


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

            latency_ms = final_time - create_time
            records.append(
                {
                    "tx_id": status.get("id"),
                    "status": tx_status,
                    "create_time_ms": create_time,
                    "final_time_ms": final_time,
                    "latency_ms": latency_ms,
                    "verified": status.get("verified"),
                    "worker_index": payload.get("workerIndex"),
                    "round_index": payload.get("roundIndex"),
                }
            )

    return pd.DataFrame.from_records(records)


def apply_trim_window(df, trim_seconds):
    if df.empty or trim_seconds <= 0:
        return df

    min_create = df["create_time_ms"].min()
    max_create = df["create_time_ms"].max()
    lower_bound = min_create + trim_seconds * 1000
    upper_bound = max_create - trim_seconds * 1000

    if upper_bound <= lower_bound:
        return df

    trimmed_df = df[
        (df["create_time_ms"] >= lower_bound) & (df["create_time_ms"] <= upper_bound)
    ].copy()
    return trimmed_df if not trimmed_df.empty else df


def compute_summary(df, target_tps):
    total = len(df)
    success = df[df["status"] == "success"].copy()
    failed = df[df["status"] != "success"].copy()

    if total == 0 or success.empty:
        return {
            "target_tps": target_tps,
            "submitted": total,
            "success": int((df["status"] == "success").sum()) if total else 0,
            "failed": int((df["status"] != "success").sum()) if total else 0,
            "failure_rate_pct": 100.0 if total else 0.0,
            "throughput_tps": 0.0,
            "mean_latency_ms": math.nan,
            "p50_latency_ms": math.nan,
            "p95_latency_ms": math.nan,
            "p99_latency_ms": math.nan,
            "max_latency_ms": math.nan,
            "latency_std_ms": math.nan,
            "spike_share_pct": math.nan,
            "instability_cv": math.nan,
        }

    elapsed_s = (success["final_time_ms"].max() - success["create_time_ms"].min()) / 1000.0
    throughput_tps = len(success) / elapsed_s if elapsed_s > 0 else 0.0
    p95 = float(np.percentile(success["latency_ms"], 95))
    median = float(np.percentile(success["latency_ms"], 50))
    spike_threshold = max(p95, median * 2.0)
    spike_share = float((success["latency_ms"] > spike_threshold).mean() * 100.0)

    time_series = build_time_series(success, target_tps)
    p95_series = time_series["p95_latency_ms"].dropna()
    instability_cv = (
        float(p95_series.std(ddof=0) / p95_series.mean())
        if len(p95_series) > 1 and p95_series.mean() > 0
        else math.nan
    )

    return {
        "target_tps": target_tps,
        "submitted": total,
        "success": int(len(success)),
        "failed": int(len(failed)),
        "failure_rate_pct": float(len(failed) / total * 100.0),
        "throughput_tps": throughput_tps,
        "mean_latency_ms": float(success["latency_ms"].mean()),
        "p50_latency_ms": median,
        "p95_latency_ms": p95,
        "p99_latency_ms": float(np.percentile(success["latency_ms"], 99)),
        "max_latency_ms": float(success["latency_ms"].max()),
        "latency_std_ms": float(success["latency_ms"].std(ddof=0)),
        "spike_share_pct": spike_share,
        "instability_cv": instability_cv,
    }


def build_time_series(success_df, target_tps):
    series_df = success_df.copy()
    zero_time = series_df["create_time_ms"].min()
    series_df["second"] = ((series_df["create_time_ms"] - zero_time) / 1000.0).astype(int)

    grouped = series_df.groupby("second")["latency_ms"]
    ts = grouped.agg(["count", "mean", "max"]).rename(
        columns={
            "count": "completed_txs",
            "mean": "mean_latency_ms",
            "max": "max_latency_ms",
        }
    )
    ts["p95_latency_ms"] = grouped.quantile(0.95)
    ts["p99_latency_ms"] = grouped.quantile(0.99)
    ts["target_tps"] = target_tps
    ts = ts.reset_index()
    return ts


def find_run_dirs(input_dir):
    input_path = pathlib.Path(input_dir).resolve()
    manifest_path = input_path / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        return [pathlib.Path(run["run_dir"]) for run in manifest["runs"]]

    return sorted(path for path in input_path.iterdir() if path.is_dir() and path.name.startswith("tps_"))


def make_percentile_plot(summary_df, output_dir):
    plt.figure(figsize=(10, 6))
    plt.plot(summary_df["target_tps"], summary_df["p50_latency_ms"], marker="o", label="p50")
    plt.plot(summary_df["target_tps"], summary_df["p95_latency_ms"], marker="o", label="p95")
    plt.plot(summary_df["target_tps"], summary_df["p99_latency_ms"], marker="o", label="p99")
    plt.plot(summary_df["target_tps"], summary_df["max_latency_ms"], marker="o", label="max", alpha=0.7)
    plt.xlabel("Target Load (TPS)")
    plt.ylabel("Latency (ms)")
    plt.title("Latency percentiles vs offered load")
    plt.grid(alpha=0.3)
    plt.legend()
    plt.tight_layout()
    plt.savefig(output_dir / "latency_percentiles_vs_tps.png", dpi=200)
    plt.close()


def make_overload_plot(summary_df, output_dir):
    fig, ax1 = plt.subplots(figsize=(10, 6))
    ax1.plot(summary_df["target_tps"], summary_df["throughput_tps"], color="#1f77b4", marker="o", label="Achieved throughput")
    ax1.plot(summary_df["target_tps"], summary_df["target_tps"], color="#7f7f7f", linestyle="--", label="Ideal throughput")
    ax1.set_xlabel("Target Load (TPS)")
    ax1.set_ylabel("Throughput (TPS)", color="#1f77b4")
    ax1.tick_params(axis="y", labelcolor="#1f77b4")
    ax1.grid(alpha=0.3)

    ax2 = ax1.twinx()
    ax2.plot(summary_df["target_tps"], summary_df["failure_rate_pct"], color="#d62728", marker="s", label="Failure rate")
    ax2.set_ylabel("Failure Rate (%)", color="#d62728")
    ax2.tick_params(axis="y", labelcolor="#d62728")

    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper left")
    plt.title("Overload behavior: throughput saturation and failures")
    plt.tight_layout()
    plt.savefig(output_dir / "overload_behavior.png", dpi=200)
    plt.close()


def make_heatmap(time_series_df, output_dir):
    heatmap = time_series_df.pivot(index="target_tps", columns="second", values="p95_latency_ms")
    plt.figure(figsize=(12, 6))
    plt.imshow(heatmap, aspect="auto", cmap="magma", interpolation="nearest")
    plt.colorbar(label="Per-second p95 latency (ms)")
    plt.yticks(range(len(heatmap.index)), heatmap.index)
    plt.xticks(range(len(heatmap.columns)), heatmap.columns)
    plt.xlabel("Second in run")
    plt.ylabel("Target Load (TPS)")
    plt.title("Latency instability heatmap")
    plt.tight_layout()
    plt.savefig(output_dir / "latency_instability_heatmap.png", dpi=200)
    plt.close()


def make_spike_plot(time_series_df, summary_df, output_dir):
    if summary_df.empty:
        return

    candidate = summary_df.sort_values(["failure_rate_pct", "p95_latency_ms", "target_tps"], ascending=[False, False, False]).iloc[0]
    target_tps = candidate["target_tps"]
    run_df = time_series_df[time_series_df["target_tps"] == target_tps]

    plt.figure(figsize=(10, 6))
    plt.plot(run_df["second"], run_df["p95_latency_ms"], marker="o", label="Per-second p95")
    plt.plot(run_df["second"], run_df["mean_latency_ms"], marker="o", alpha=0.7, label="Per-second mean")
    plt.xlabel("Second in run")
    plt.ylabel("Latency (ms)")
    plt.title(f"Latency spikes at {int(target_tps)} TPS")
    plt.grid(alpha=0.3)
    plt.legend()
    plt.tight_layout()
    plt.savefig(output_dir / "latency_spikes_representative_run.png", dpi=200)
    plt.close()


def detect_overload(summary_df):
    for _, row in summary_df.sort_values("target_tps").iterrows():
        if row["failure_rate_pct"] > 1.0:
            return row["target_tps"], "failure rate exceeded 1%"
        if row["throughput_tps"] < row["target_tps"] * 0.9:
            return row["target_tps"], "achieved throughput dropped below 90% of offered load"
    return None, "no clear overload point in tested range"


def write_markdown_report(summary_df, output_dir):
    overload_tps, overload_reason = detect_overload(summary_df)
    best_stable = summary_df.sort_values("target_tps")
    stable_rows = best_stable[
        (best_stable["failure_rate_pct"] <= 1.0)
        & (best_stable["throughput_tps"] >= best_stable["target_tps"] * 0.9)
    ]
    stable_tps = int(stable_rows["target_tps"].max()) if not stable_rows.empty else None

    lines = [
        "# Latency Analysis",
        "",
        "## Key findings",
    ]
    if stable_tps is not None:
        stable_row = stable_rows.sort_values("target_tps").iloc[-1]
        lines.append(
            f"- Highest stable load observed: {stable_tps} TPS with p95 latency {stable_row['p95_latency_ms']:.1f} ms and failure rate {stable_row['failure_rate_pct']:.2f}%."
        )
    if overload_tps is not None:
        overload_row = summary_df[summary_df["target_tps"] == overload_tps].iloc[0]
        lines.append(
            f"- Overload knee appears at {int(overload_tps)} TPS because {overload_reason}; p95 latency reached {overload_row['p95_latency_ms']:.1f} ms."
        )
    else:
        lines.append(f"- Overload knee not observed in the tested load range; {overload_reason}.")

    noisiest = summary_df.sort_values("instability_cv", ascending=False).iloc[0]
    lines.append(
        f"- Most unstable run: {int(noisiest['target_tps'])} TPS with per-second p95 CV {noisiest['instability_cv']:.3f}."
    )
    spikiest = summary_df.sort_values("spike_share_pct", ascending=False).iloc[0]
    lines.append(
        f"- Highest spike share: {int(spikiest['target_tps'])} TPS with {spikiest['spike_share_pct']:.2f}% of successful transactions above the spike threshold."
    )
    lines.append("")
    lines.append("## Artifacts")
    lines.append("- `summary.csv`: percentile and throughput table for each load point.")
    lines.append("- `time_series.csv`: per-second latency series used for instability and spike plots.")
    lines.append("- `latency_percentiles_vs_tps.png`: p50/p95/p99/max vs offered load.")
    lines.append("- `overload_behavior.png`: achieved throughput and failure rate vs offered load.")
    lines.append("- `latency_instability_heatmap.png`: per-second p95 heatmap across load points.")
    lines.append("- `latency_spikes_representative_run.png`: representative spike trace from the noisiest or overloaded run.")
    (output_dir / "analysis.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    args = parse_args()
    input_dir = pathlib.Path(args.input_dir).resolve()
    os.environ.setdefault("MPLCONFIGDIR", str(input_dir / ".mpl-cache"))

    run_dirs = find_run_dirs(input_dir)
    summaries = []
    time_series_frames = []

    for run_dir in run_dirs:
        metadata_path = run_dir / "metadata.json"
        log_path = run_dir / "run.log"
        if not metadata_path.exists() or not log_path.exists():
            continue

        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        target_tps = metadata["target_tps"]
        tx_df = extract_tx_records(log_path)
        if tx_df.empty:
            continue

        tx_df = apply_trim_window(tx_df, metadata.get("trim", 0))

        summary = compute_summary(tx_df, target_tps)
        summaries.append(summary)

        success_df = tx_df[tx_df["status"] == "success"].copy()
        if not success_df.empty:
            ts = build_time_series(success_df, target_tps)
            ts["run_dir"] = str(run_dir)
            time_series_frames.append(ts)

    if not summaries:
        raise SystemExit("No transaction logs found. Make sure the sweep completed successfully.")

    summary_df = pd.DataFrame(summaries).sort_values("target_tps").reset_index(drop=True)
    time_series_df = pd.concat(time_series_frames, ignore_index=True) if time_series_frames else pd.DataFrame()

    summary_df.to_csv(input_dir / "summary.csv", index=False)
    if not time_series_df.empty:
        time_series_df.to_csv(input_dir / "time_series.csv", index=False)

    make_percentile_plot(summary_df, input_dir)
    make_overload_plot(summary_df, input_dir)
    if not time_series_df.empty:
        make_heatmap(time_series_df, input_dir)
        make_spike_plot(time_series_df, summary_df, input_dir)
    write_markdown_report(summary_df, input_dir)

    print(f"Analysis written to {input_dir}")


if __name__ == "__main__":
    main()
