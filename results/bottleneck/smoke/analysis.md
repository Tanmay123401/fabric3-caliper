# Bottleneck Stage Analysis

## Key findings
- Highest stable load observed: 50 TPS with bottleneck stage `commit` at p95.
- First overload point observed: 50 TPS, where achieved throughput was 49.8 TPS and the dominant p95 stage was `commit`.
- Largest commit-stage delay: 50 TPS with p95 commit delay 288.0 ms.
- Highest average CPU load was on `peer0.org1.example.com` at 50 TPS with 43.7% CPU.
- Most timeout-related log activity came from `peer0.org1.example.com` at 50 TPS with 600 matching log lines.

## Artifacts
- `stage_summary.csv`: stage-delay summary per load point.
- `resource_summary.csv`: averaged Docker CPU/memory metrics per container and load point.
- `log_summary.csv`: keyword-hit summary from peer/orderer logs.
- `stage_delay_p95_vs_tps.png`: p95 endorsement, ordering, and commit delays vs load.
- `mean_latency_decomposition.png`: stacked mean latency decomposition by stage.
- `resource_utilization_vs_tps.png`: average CPU and memory by container and load.
- `timeout_log_heatmap.png`: timeout-related log intensity across containers and load points.
