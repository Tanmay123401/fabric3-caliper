# Latency Analysis

## Key findings
- Overload knee appears at 50 TPS because failure rate exceeded 1%; p95 latency reached nan ms.
- Most unstable run: 50 TPS with per-second p95 CV nan.
- Highest spike share: 50 TPS with nan% of successful transactions above the spike threshold.

## Artifacts
- `summary.csv`: percentile and throughput table for each load point.
- `time_series.csv`: per-second latency series used for instability and spike plots.
- `latency_percentiles_vs_tps.png`: p50/p95/p99/max vs offered load.
- `overload_behavior.png`: achieved throughput and failure rate vs offered load.
- `latency_instability_heatmap.png`: per-second p95 heatmap across load points.
- `latency_spikes_representative_run.png`: representative spike trace from the noisiest or overloaded run.
