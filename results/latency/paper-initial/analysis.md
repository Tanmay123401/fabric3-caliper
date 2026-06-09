# Latency Analysis

## Key findings
- Highest stable load observed: 200 TPS with p95 latency 100.0 ms and failure rate 0.00%.
- Overload knee appears at 250 TPS because achieved throughput dropped below 90% of offered load; p95 latency reached 4981.8 ms.
- Most unstable run: 100 TPS with per-second p95 CV 0.482.
- Highest spike share: 150 TPS with 5.12% of successful transactions above the spike threshold.

## Artifacts
- `summary.csv`: percentile and throughput table for each load point.
- `time_series.csv`: per-second latency series used for instability and spike plots.
- `latency_percentiles_vs_tps.png`: p50/p95/p99/max vs offered load.
- `overload_behavior.png`: achieved throughput and failure rate vs offered load.
- `latency_instability_heatmap.png`: per-second p95 heatmap across load points.
- `latency_spikes_representative_run.png`: representative spike trace from the noisiest or overloaded run.
