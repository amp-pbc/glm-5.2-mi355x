import asyncio
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import httpx

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


render = module("render", "scripts/render.py")
stage = module("stage", "scripts/stage.py")
replay = module("replay", "bench/replay.py")


class RecipeTests(unittest.TestCase):
    def test_eight_distinct_gpu_workers_and_valid_mounts(self):
        deployments = [d for d in render.render() if d["kind"] == "Deployment"]
        requested = 0
        for d in deployments:
            pod = d["spec"]["template"]["spec"]
            volumes = {v["name"] for v in pod["volumes"]}
            self.assertFalse(any("persistentVolumeClaim" in v for v in pod["volumes"]))
            for container in pod["containers"] + pod["initContainers"]:
                for mount in container.get("volumeMounts", []):
                    self.assertIn(mount["name"], volumes)
            gpu = pod["containers"][0]["resources"]["requests"].get("amd.com/gpu", 0)
            requested += d["spec"]["replicas"] * int(gpu)
            if gpu:
                self.assertTrue(pod["affinity"]["podAntiAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"])
                self.assertNotIn("kubernetes.io/hostname", pod["nodeSelector"])
                self.assertEqual(pod["initContainers"][-1]["name"], "kvd")
                self.assertIn("startupProbe", pod["initContainers"][-1])
        self.assertEqual(requested, 64)

    def test_staging_rejects_wrong_revision_without_copying(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir()
            (source / stage.RECEIPT).write_text(json.dumps({"revision": "wrong", "repo": stage.REPO, "files": {}}))
            with self.assertRaisesRegex(RuntimeError, "revision"):
                stage.stage(root / "target", source)
            self.assertFalse((root / "target" / stage.RECEIPT).exists())

    def test_staging_rejects_truncated_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "weights").write_bytes(b"x")
            (root / stage.RECEIPT).write_text(json.dumps({"revision": stage.REVISION, "repo": stage.REPO, "files": {"weights": 2}}))
            with self.assertRaisesRegex(RuntimeError, "incomplete"):
                stage.stage(root / "target", root)


class ReplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_sessions_are_causal_and_steps_are_drained(self):
        inflight = 0
        replies = {}

        async def handler(request):
            nonlocal inflight
            body = json.loads(request.content)
            sid = body["messages"][0]["content"]
            if sid in replies:
                self.assertEqual(body["messages"][-2], {"role": "assistant", "content": replies[sid]})
            inflight += 1
            await asyncio.sleep(0.005)
            reply = "response-" + str(len(body["messages"]))
            replies[sid] = reply
            inflight -= 1
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 3}})

        args = SimpleNamespace(url="http://test", output_tokens=128, timeout=1, nodes=8, run_id="test")
        records = [{"id": str(i), "turns": [str(i), "follow-up"]} for i in range(6)]
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            summary = await replay.run_step(client, records, 3, args, io.StringIO())
        self.assertEqual(inflight, 0)
        self.assertEqual(summary["completed_requests"], 12)
        self.assertEqual(summary["output_tokens"], 36)
        self.assertEqual(summary["output_tokens_per_second_per_node"], summary["output_tokens_per_second"] / 8)

    async def test_failure_skips_dependent_turn_and_is_counted(self):
        async def handler(request):
            return httpx.Response(500)
        args = SimpleNamespace(url="http://test", output_tokens=128, timeout=1, nodes=8, run_id="test")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await replay.run_step(client, [{"id": "a", "turns": ["first", "second"]}], 1, args, io.StringIO())
        self.assertEqual(result["failed_requests"], 1)
        self.assertEqual(result["skipped_requests"], 1)
        self.assertEqual(result["output_tokens_per_second"], 0)


if __name__ == "__main__":
    unittest.main()
