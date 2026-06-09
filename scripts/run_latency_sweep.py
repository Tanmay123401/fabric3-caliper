#!/usr/bin/env python3

import argparse
import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
import textwrap


ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = ROOT / "results" / "latency"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run a Caliper latency sweep and capture per-transaction logs."
    )
    parser.add_argument(
        "--tps",
        nargs="+",
        type=int,
        default=[50, 100, 150, 200, 250],
        help="Target TPS sweep values.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=10,
        help="Number of Caliper workers.",
    )
    parser.add_argument(
        "--tx-duration",
        type=int,
        default=20,
        help="Benchmark duration in seconds for each TPS point.",
    )
    parser.add_argument(
        "--trim",
        type=int,
        default=5,
        help="Warmup/cooldown trim window in seconds.",
    )
    parser.add_argument(
        "--payload-kb",
        type=int,
        default=1,
        help="Payload size passed to the workload.",
    )
    parser.add_argument(
        "--label",
        default="createAsset",
        help="Base round label to use in generated benchmark files.",
    )
    parser.add_argument(
        "--network-config",
        default="network.yaml",
        help="Network config path relative to the Caliper workspace.",
    )
    parser.add_argument(
        "--output-dir",
        default="",
        help="Optional output directory. Defaults to results/latency/<timestamp>.",
    )
    return parser.parse_args()


def render_benchmark_yaml(args, target_tps):
    return textwrap.dedent(
        f"""\
        name: Latency-Sweep

        monitors:
          transaction:
            - module: logging
              options:
                loggerModuleName: txinfo
                messageLevel: info

        test:
          workers:
            number: {args.workers}
          rounds:
            - label: {args.label}_tps_{target_tps}
              description: Latency sweep point at {target_tps} TPS
              txDuration: {args.tx_duration}
              trim: {args.trim}
              rateControl:
                type: fixed-rate
                opts:
                  tps: {target_tps}
              workload:
                module: workloads/createAsset.js
                arguments:
                  payloadKB: {args.payload_kb}
        """
    )


def ensure_dir(path):
    path.mkdir(parents=True, exist_ok=True)


def write_text(path, content):
    path.write_text(content, encoding="utf-8")


def run_point(args, sweep_dir, target_tps):
    run_dir = sweep_dir / f"tps_{target_tps}"
    ensure_dir(run_dir)

    benchmark_path = run_dir / "benchmark.yaml"
    report_path = run_dir / "report.html"
    log_path = run_dir / "run.log"
    metadata_path = run_dir / "metadata.json"

    write_text(benchmark_path, render_benchmark_yaml(args, target_tps))
    metadata = {
        "target_tps": target_tps,
        "workers": args.workers,
        "tx_duration": args.tx_duration,
        "trim": args.trim,
        "payload_kb": args.payload_kb,
        "benchmark_path": str(benchmark_path),
        "report_path": str(report_path),
        "network_config": args.network_config,
    }
    write_text(metadata_path, json.dumps(metadata, indent=2))

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

    with log_path.open("w", encoding="utf-8") as log_file:
        cache_dir = sweep_dir / ".cache"
        ensure_dir(cache_dir)
        process = subprocess.run(
            cmd,
            cwd=ROOT,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
            env={
                **os.environ,
                "MPLCONFIGDIR": str(sweep_dir / ".mpl-cache"),
                "XDG_CACHE_HOME": str(cache_dir),
            },
        )

    if process.returncode != 0:
        raise RuntimeError(
            f"Caliper run failed for {target_tps} TPS. See {log_path} for details."
        )

    return run_dir


def main():
    args = parse_args()
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    sweep_dir = pathlib.Path(args.output_dir) if args.output_dir else DEFAULT_OUTPUT_ROOT / timestamp
    if not sweep_dir.is_absolute():
        sweep_dir = ROOT / sweep_dir
    ensure_dir(sweep_dir)

    manifest = {
        "created_at": dt.datetime.now().isoformat(),
        "runs": [],
    }

    for target_tps in args.tps:
        print(f"Running latency sweep point at {target_tps} TPS...", flush=True)
        run_dir = run_point(args, sweep_dir, target_tps)
        manifest["runs"].append(
            {
                "target_tps": target_tps,
                "run_dir": str(run_dir),
            }
        )

    manifest_path = sweep_dir / "manifest.json"
    write_text(manifest_path, json.dumps(manifest, indent=2))

    print(f"Latency sweep completed. Results written to {sweep_dir}")
    print(
        "Analyze with: "
        f"python3 {ROOT / 'scripts' / 'analyze_latency.py'} --input-dir {sweep_dir}"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
