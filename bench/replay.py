"""Causal session replay with fully drained concurrency steps.

JSONL records: {"id": "unique-session-id", "turns": ["prompt", "follow-up"]}
or {"id": "unique-request-id", "messages": [{"role": "user", "content": "..."}]}.
Messages records replay independently; do not put dependent turns in separate records.
"""
import argparse
import asyncio
import json
from pathlib import Path
import statistics
import time
import httpx


async def run_step(client, records, concurrency, args, out):
    semaphore = asyncio.Semaphore(concurrency)
    receipts = []

    async def session(record):
        # Hold a slot for a whole session. Each next turn includes the actual
        # assistant reply and cannot start while its predecessor is in flight.
        async with semaphore:
            messages = list(record.get("messages", []))
            turns = record.get("turns", [None])
            for turn, prompt in enumerate(turns):
                if prompt is not None:
                    messages.append({"role": "user", "content": prompt})
                receipt = {"session": record["id"], "turn": turn, "concurrency": concurrency}
                start = time.perf_counter()
                try:
                    response = await asyncio.wait_for(client.post(
                        args.url.rstrip("/") + "/v1/chat/completions",
                        json={"model": "glm5.2-mxfp4", "messages": messages,
                              "max_tokens": args.output_tokens, "temperature": 0,
                              "stream": False},
                        headers={"X-Request-ID": f"{args.run_id}-c{concurrency}-{record['id']}-t{turn}"},
                    ), timeout=args.timeout)
                    response.raise_for_status()
                    data = response.json()
                    choice = data["choices"][0]
                    usage = data["usage"]
                    receipt.update(ok=True, usage=usage, finish_reason=choice.get("finish_reason"),
                                   message=choice["message"], response_id=data.get("id"))
                    messages.append(choice["message"])
                except Exception as exc:
                    receipt.update(ok=False, error=f"{type(exc).__name__}: {exc}")
                receipt["seconds"] = time.perf_counter() - start
                receipts.append(receipt)
                out.write(json.dumps(receipt) + "\n")
                out.flush()
                if not receipt["ok"]:
                    break  # A failed predecessor invalidates subsequent turns.

    started = time.perf_counter()
    await asyncio.gather(*(session(record) for record in records))
    elapsed = time.perf_counter() - started
    good = [r for r in receipts if r["ok"]]
    prompt_tokens = sum(r["usage"]["prompt_tokens"] for r in good)
    output_tokens = sum(r["usage"]["completion_tokens"] for r in good)
    expected = sum(len(r.get("turns", [None])) for r in records)
    summary = {"concurrency": concurrency, "elapsed_seconds": elapsed,
               "expected_requests": expected, "completed_requests": len(good),
               "failed_requests": sum(not r["ok"] for r in receipts),
               "skipped_requests": expected - len(receipts),
               "prompt_tokens": prompt_tokens, "output_tokens": output_tokens,
               "output_tokens_per_second": output_tokens / elapsed,
               "input_plus_output_tokens_per_second": (prompt_tokens + output_tokens) / elapsed,
               "output_tokens_per_second_per_node": output_tokens / elapsed / args.nodes,
               "allocated_gpu_nodes": args.nodes,
               "length_capped_requests": sum(r.get("finish_reason") == "length" for r in good),
               "mean_request_seconds": statistics.mean(r["seconds"] for r in good) if good else None}
    print(json.dumps(summary), flush=True)
    return summary


async def main(args):
    records = [json.loads(line) for line in args.input.read_text().splitlines() if line.strip()]
    if not records or len({r["id"] for r in records}) != len(records):
        raise ValueError("Input must contain records with unique session IDs")
    for r in records:
        if ("turns" in r) == ("messages" in r) or not r.get("turns", r.get("messages")):
            raise ValueError("Each record needs exactly one nonempty turns or messages array")
    args.results.mkdir(parents=True, exist_ok=False)
    (args.results / "config.json").write_text(json.dumps({k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()}, indent=2))
    summaries = []
    async with httpx.AsyncClient(timeout=args.timeout, limits=httpx.Limits(max_connections=max(args.concurrency))) as client:
        for concurrency in args.concurrency:
            with (args.results / f"c{concurrency}.jsonl").open("w") as out:
                summaries.append(await run_step(client, records, concurrency, args, out))
            # gather has returned: nothing from this step is left running.
    (args.results / "summary.json").write_text(json.dumps(summaries, indent=2))
    if any(s["failed_requests"] or s["skipped_requests"] for s in summaries):
        raise SystemExit("Replay had failures; this is not a passing throughput result")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", default="http://127.0.0.1:8000")
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--results", type=Path, required=True)
    p.add_argument("--concurrency", type=int, nargs="+", default=[8, 32, 96])
    p.add_argument("--output-tokens", type=int, default=128)
    p.add_argument("--timeout", type=float, default=600)
    p.add_argument("--nodes", type=int, default=8)
    p.add_argument("--run-id", default=str(time.time_ns()))
    a = p.parse_args()
    if a.nodes < 1 or min(a.concurrency) < 1 or len(set(a.concurrency)) != len(a.concurrency):
        p.error("Nodes/concurrency must be positive; concurrency steps must be unique")
    asyncio.run(main(a))
