# Validation record and procedure

2026-09-18: recipe preparation only. Customer authentication is pending. No GPU nodes have been allocated by this GLM test and no live results are available.

Local validation passed: five unit tests covering the 64-GPU manifest, mount references, stage receipt rejection, causal replay, and failure accounting; Python compilation; registry lookup of both pinned serving image digests. These checks do not execute ROCm, SGLang, Mooncake or kvd and do not replace a Kubernetes server dry run.

The [upstream recipe README at the audited revision](https://github.com/AMD-AGI/Infera/blob/625a950b109371aaf8ebedfdc05757b57ac32eab/examples/recipes/glm5.2/README.md) marks disaggregated + kvd as not run. Its plain disaggregated cross-node result required replacing the shared local-path model PVC. Its SGLang kvd tier evidence uses Qwen3-0.6B, so it does not establish GLM PD+kvd correctness. This repository addresses the storage/deployment structure; runtime validation remains necessary.

## Required receipts

1. **Preflight:** customer cluster identity, capacity and Kubernetes sidecar support; shared/local free space; actual image digests and package versions; all eight distinct node identities; RDMA devices/GID/PeerDirect availability. Keep private addresses and provider identifiers out of published reports.
2. **Startup:** both roles healthy on all eight nodes, no numerical corruption or recurrent worker restarts, required Mooncake native module present. Check worker annotations advertise routable worker, bootstrap, KV event and snapshot addresses. Verify router tokenizer loading and all eight workers discovered. No TCP fallback allowed.
3. **Correctness:** several deterministic factual and structured-output prompts on each worker pair, then long inputs and real multi-turn prompts. Use generous output budgets and inspect reasoning/content separation. Track truncation separately from wrong answers. Repeat after sustained load.
4. **PD transfer:** correlated request identifiers and worker logs/metrics show prefill and decode handled different stages. Capture RDMA transfer counters and timings, plus a no-fallback log check. A configured Mooncake flag alone is insufficient.
5. **KV-aware routing:** capture every worker's cumulative request/token/cache counters before and after each arm. First send independent short prompts under concurrency, then distinct long sessions with repeated prefixes. Record the worker's actual advertised block size. Verify prefix-event/snapshot health and engine cache reuse. Compare kv-aware and round-robin using the same workload and controlled cache state. Report per-worker deltas and, where exposed, actual per-request worker assignments; aggregate counters do not identify every request's route.
6. **kvd:** for each of the eight Pods, run the command below before/after sufficiently long prompts. Require nonzero `entries` and `long_bytes`, and demonstrate reads on replay after the corresponding GPU prefix is evicted using the backend's supported cache management. Stored bytes alone prove writes, not useful cache hits. If a role cannot populate/read kvd, report that failure explicitly.
7. **Throughput:** warm up separately, fully drain, then test multiple input/output lengths and concurrency levels. Include input-heavy long contexts, 128-token output, and realistic thinking budgets. Run at least three repetitions with balanced arm order. Record actual prompt/completion tokens, cache state, failures, truncation, elapsed time, per-worker load, cluster tok/s and per-node tok/s. The default replay reports mean request latency; collect streaming timing separately if TTFT/ITL or SLO claims are needed.
8. **Cleanup:** capture zero run-owned GPU Pods and pending demands after deletion, and separately verify allocation/billing release through the customer interface. Never delete nodes to accelerate release.

```bash
kubectl --context "$CUSTOMER_CONTEXT" get pods -l app.kubernetes.io/name=glm52-worker -o wide
kubectl --context "$CUSTOMER_CONTEXT" exec POD_NAME -c kvd -- \
  /overlay/bin/infera-exec python3 -m infera.kvd.statctl --socket /kvd/kvd.sock
```

Stop on incorrect output, RDMA fallback, disk pressure, repeated worker failure, or absent cache behavior. Fix the customer recipe or record an upstream blocker before expanding the load. Do not silently disable kvd or switch topology and call the requested combination validated.
