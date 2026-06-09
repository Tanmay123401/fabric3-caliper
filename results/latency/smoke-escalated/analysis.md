# Latency Analysis

## Key findings
- Highest stable load observed: 50 TPS with p95 latency 2208.1 ms and failure rate 0.00%.
- Overload knee not observed in the tested load range; no clear overload point in tested range.
- Most unstable run: 50 TPS with per-second p95 CV 1.115.
- Highest spike share: 50 TPS with 5.00% of successful transactions above the spike threshold.

## Artifacts
- `summary.csv`: percentile and throughput table for each load point.
- `time_series.csv`: per-second latency series used for instability and spike plots.
- `latency_percentiles_vs_tps.png`: p50/p95/p99/max vs offered load.
- `overload_behavior.png`: achieved throughput and failure rate vs offered load.
- `latency_instability_heatmap.png`: per-second p95 heatmap across load points.
- `latency_spikes_representative_run.png`: representative spike trace from the noisiest or overloaded run.
