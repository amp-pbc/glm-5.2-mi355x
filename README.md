# GLM-5.2-MXFP4 on eight MI355X nodes

Customer Kubernetes recipe for AMD's `amd/GLM-5.2-MXFP4`, using stock SGLang with a mounted Infera overlay. The initial layout is **four prefill nodes and four decode nodes**, TP8 on every node, with kvd on both roles: eight nodes / 64 GPUs total. This is eight independent TP8 workers, not one model using TP64.

**Status: customer CPU preflight passed; GPU validation pending market admission.** No eight-node correctness, KV-transfer, cache, or throughput result is claimed. See [validation status](docs/validation.md).

Adapted from [AMD's recipe](https://rocm.docs.amd.com/projects/infera/en/latest/recipes/glm5.2.html). The exact upstream manifest is preserved in [upstream/disaggregated-kvd.yaml](upstream/disaggregated-kvd.yaml). [Provenance and licenses](THIRD_PARTY_NOTICES.md) list the immutable source/image/model pins.

## Requirements

- A National Compute customer cluster with MI355X capacity, a CPU worker, and a shared volume mounted at `/mnt/shared`. Run company MI355X tests only in `amp-dev`.
- Eight available GPU nodes, eight GPUs each, routable RoCE/PeerDirect fabric, local NVMe at `/mnt/nvme`, and host `/dev/infiniband`. Each worker requests 32 CPUs and 512 GiB RAM plus its kvd sidecar. Verify capacity before launching.
- Enough **unused** shared storage for the 438,033,728,823-byte checkpoint plus 20 GiB reserve, and enough local NVMe per node for the checkpoint plus up to 100 GiB of L3 cache. Preserve other checkpoints.
- Kubernetes with native sidecar support. The kvd sidecar has a startup probe so the engine cannot race its socket.
- Python 3.10+, `kubectl`, and `pip install -r requirements.txt` locally. Always choose your **customer** context explicitly.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 -m unittest discover -s tests -v
```

This draft carries the previously used customer fabric's GID index 1 and host libionic mount. Verify those assumptions in the current tenant before deploying; they are not universal ROCm settings. Do not use an admin context or modify node/platform configuration to make this recipe work.

## Deploy

```bash
export CUSTOMER_CONTEXT=your-customer-context
kubectl --context "$CUSTOMER_CONTEXT" get nodes -o wide
python3 scripts/render.py --phase stage > stage.yaml
kubectl --context "$CUSTOMER_CONTEXT" apply -f stage.yaml
kubectl --context "$CUSTOMER_CONTEXT" logs -f job/glm52-weights
kubectl --context "$CUSTOMER_CONTEXT" wait --for=condition=complete job/glm52-weights --timeout=4h

# Run only after staging succeeds. This requests 64 GPUs.
python3 scripts/render.py --phase serve --prefill 4 --decode 4 > serve.yaml
kubectl --context "$CUSTOMER_CONTEXT" apply -f serve.yaml
kubectl --context "$CUSTOMER_CONTEXT" get pods -l app.kubernetes.io/part-of=glm52-recipe -o wide
kubectl --context "$CUSTOMER_CONTEXT" rollout status deployment/glm52-prefill --timeout=2h
kubectl --context "$CUSTOMER_CONTEXT" rollout status deployment/glm52-decode --timeout=2h
kubectl --context "$CUSTOMER_CONTEXT" port-forward svc/glm52-router 8000:8000
```

Keep port-forward running in a separate terminal. The serving manifest includes a CPU router, discovery RBAC, and ordinary Deployments; no Infera operator or platform changes are required. Pod anti-affinity enforces distinct nodes. Fixed hostnames and RWO local-path PVCs from the upstream example are replaced with customer node selection and per-node NVMe. The `Recreate` strategy avoids requesting extra GPUs during a rollout.

For a new image/fabric combination, first render `--prefill 1 --decode 1` and verify the pair before applying the full eight-node manifest. If workers remain `SchedulingGated`, inspect `kubectl --context "$CUSTOMER_CONTEXT" get workloads -o yaml`: a market bid rejection needs a customer budget decision in Burst Capacity. The eight-node hourly ceiling is 64 times the per-GPU hourly limit. Do not change infrastructure or remove scheduling gates to bypass admission.

The renderer also supports `--workload-kind job` for bounded tests: one independent GPU Job per node, no automatic retries, and a four-hour deadline. This mode is not a persistent Deployment. Its optional `--limit-price YOUR_CEILING` writes the documented per-Job price label. **The live preflight did not observe admission honoring that label.** Read the actual admission verdict and verify the standing customer price before running; do not rely on the label as a verified spending cap. Switching workload kinds requires deleting the previous serving manifest first to avoid duplicate GPU demand.

Workers copy the pinned checkpoint from shared storage to local NVMe. The router uses the same checkpoint's tokenizer. The engine flags explicitly enable the ROCm TileLang DSA path and `glm45` reasoning parser. FP8 KV, a 32K context, 8192 prefill token budget, and 24 running requests per worker initially match the upstream recipe. These are starting settings, not optimized throughput claims.

## Verify before benchmarking

```bash
curl --fail-with-body http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"glm5.2-mxfp4","messages":[{"role":"user","content":"What is the capital of France?"}],"max_tokens":512,"temperature":0}'
```

Expect a coherent answer identifying Paris, with reasoning separated from `content`. A single answer is only a smoke test. Complete the [validation procedure](docs/validation.md) before making a performance claim. In particular, enabling kv-aware routing, transferring KV between roles, and storing KV in kvd are three separate behaviors to verify.

## Input-heavy load test

`bench/replay.py` accepts JSONL chat requests or causal multi-turn sessions. Use an approved corpus; request/response receipts may contain its full content and belong in ignored `.results/` only.

```json
{"id":"session-001","turns":["A long document followed by a question...","Now summarize the evidence for that answer."]}
{"id":"request-002","messages":[{"role":"user","content":"Another independent long prompt..."}]}
```

```bash
python3 bench/replay.py --input .results/input-heavy.jsonl \
  --results .results/replay-001 --concurrency 8 32 96 --nodes 8 --output-tokens 128
```

Each concurrency step fully drains; turns within a session cannot overlap. Throughput uses returned usage counts and full elapsed time, including failures. Report output tok/s separately from input-plus-output tok/s, and divide cluster throughput by **all eight GPU nodes**, including prefill nodes. Length-capped answers are counted explicitly. The harness does not independently certify response correctness, cold-cache state, or routing; those require the separate validation receipts. Reusing a corpus warms caches, so use separate cold and warm arms and record their order. Do not compare those arms as if only routing changed.

## Clean up

```bash
# Use the exact manifests from this run. Only run-owned objects should match.
kubectl --context "$CUSTOMER_CONTEXT" delete -f serve.yaml --ignore-not-found
kubectl --context "$CUSTOMER_CONTEXT" delete -f stage.yaml --ignore-not-found
kubectl --context "$CUSTOMER_CONTEXT" get pods -l app.kubernetes.io/part-of=glm52-recipe
```

Do not delete or cordon nodes. Confirm customer allocations/billing release separately; zero Pods does not prove the provider's billing hold has elapsed. Shared weights and node-local cache files are retained; storage charges can continue. Do not delete unrelated model files or shared storage.
