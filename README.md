# Fabric3 Caliper Latency Analysis

This workspace includes a latency sweep workflow for paper-ready percentile analysis.

## Run the sweep

```bash
cd /Users/tanmay/fabric-3-research/fabric3-caliper
npm run latency:sweep
```

Useful flags:

```bash
python3 scripts/run_latency_sweep.py --tps 50 100 150 200 250 --tx-duration 20 --trim 5
```

The sweep stores one subdirectory per load point under `results/latency/<timestamp>/` and captures:

- generated benchmark config
- Caliper HTML report
- full Caliper console log
- per-run metadata

## Analyze results

```bash
cd /Users/tanmay/fabric-3-research/fabric3-caliper
python3 scripts/analyze_latency.py --input-dir results/latency/<timestamp>
```

The analyzer produces:

- `summary.csv`
- `time_series.csv`
- `analysis.md`
- `latency_percentiles_vs_tps.png`
- `overload_behavior.png`
- `latency_instability_heatmap.png`
- `latency_spikes_representative_run.png`

## What the analysis captures

- p50, p95, p99, and max latency
- throughput saturation under load
- failure-rate growth under overload
- per-second p95 instability
- latency spike concentration

## Bottleneck stage analysis

Run the stage-timed Fabric pipeline benchmark:

```bash
cd /Users/tanmay/fabric-3-research/fabric3-caliper
node scripts/run_bottleneck_analysis.js --tps 50 100 150 200 250 --duration 8 --output-dir results/bottleneck/my-run
python3 scripts/analyze_bottleneck.py --input-dir /Users/tanmay/fabric-3-research/fabric3-caliper/results/bottleneck/my-run
```

This workflow captures:

- per-transaction stage timings for endorsement, ordering, and commit
- per-load Docker CPU and memory snapshots for peers and orderer
- peer and orderer logs for each load window

Paper-facing outputs are copied to:

- `/Users/tanmay/fabric-3-research/paper-results/bottleneck-analysis/<run-name>/`

## Resource efficiency analysis

After bottleneck outputs already exist, compute resource efficiency without rerunning the benchmark:

```bash
cd /Users/tanmay/fabric-3-research/fabric3-caliper
python3 scripts/analyze_resource_efficiency.py \
  --input-dir /Users/tanmay/fabric-3-research/paper-results/bottleneck-analysis/paper-initial \
  --output-dir /Users/tanmay/fabric-3-research/paper-results/resource-efficiency-analysis/paper-initial
```

This reuses `stage_summary.csv` and `resource_summary.csv` to produce CPU efficiency, memory efficiency, scalability efficiency, and per-transaction resource cost plots.

## Dynamic worker scaling

Run the same Caliper workload while varying only the number of Caliper workers:

```bash
cd /Users/tanmay/fabric-3-research/fabric3-caliper
python3 scripts/run_dynamic_worker_scaling.py
```

Defaults:

- workers: `1 5 10 20 40`
- target TPS: `200`
- transactions per run: `1000`
- payload size: `1 KB`

Outputs are written to `/Users/tanmay/fabric-3-research/paper-results/dynamic-worker-scaling/`.

## Queueing theory analysis

Compute M/M/1 queueing metrics from existing benchmark summaries:

```bash
cd /Users/tanmay/fabric-3-research/fabric3-caliper
python3 scripts/analyze_queueing_theory.py
```

Outputs are written to `/Users/tanmay/fabric-3-research/paper-results/queueing-analysis/`.

## Energy efficiency analysis

Compute CPU, memory, and composite resource efficiency from existing benchmark and resource summaries:

```bash
cd /Users/tanmay/fabric-3-research/fabric3-caliper
python3 scripts/analyze_energy_efficiency.py
```

Outputs are written to `/Users/tanmay/fabric-3-research/paper-results/energy-efficiency-analysis/`.

## Adaptive dynamic worker scaling

Run a 10-second control-loop framework that adjusts the Caliper worker count between control windows:

```bash
cd /Users/tanmay/fabric-3-research/fabric3-caliper
python3 scripts/run_adaptive_worker_scaling.py
```

Defaults:

- initial workers: `5`
- min workers: `1`
- max workers: `50`
- target TPS: `250`
- payload size: `1 KB`
- control interval: `10 seconds`

Outputs are written to `/Users/tanmay/fabric-3-research/paper-results/dynamic-worker-scaling/`.
