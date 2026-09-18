"""Download a pinned checkpoint, or copy it to a worker's local NVMe.

The completion receipt is written only after every remote file size matches.
Existing unrelated model directories are never removed.
"""
import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import shutil
import urllib.request

REPO = "amd/GLM-5.2-MXFP4"
REVISION = "386bd0e4ec821f7b07975701cec3c3b953a5576a"
RECEIPT = ".glm52-complete.json"
RESERVE = 20 * 1024**3


def verify(root, files):
    return all((root / p).is_file() and (root / p).stat().st_size == n
               for p, n in files.items())


def stage(target, source=None):
    target.mkdir(parents=True, exist_ok=True)
    if source:
        receipt = json.loads((source / RECEIPT).read_text())
        if receipt["revision"] != REVISION or receipt["repo"] != REPO:
            raise RuntimeError("Shared checkpoint revision does not match recipe")
        files = receipt["files"]
        if not verify(source, files):
            raise RuntimeError("Shared checkpoint is incomplete")
    else:
        with urllib.request.urlopen(
            f"https://huggingface.co/api/models/{REPO}/revision/{REVISION}?blobs=true",
            timeout=60,
        ) as response:
            metadata = json.load(response)
        if metadata["sha"] != REVISION:
            raise RuntimeError("Unexpected model revision")
        files = {f["rfilename"]: f["size"] for f in metadata["siblings"]}
    pending = [p for p, n in files.items()
               if not (target / p).is_file() or (target / p).stat().st_size != n]
    needed = sum(files[p] for p in pending)
    free = shutil.disk_usage(target).free
    if free < needed + RESERVE:
        raise RuntimeError(f"Need {needed + RESERVE:,} free bytes; available {free:,}. "
                           "Preserve other checkpoints; choose storage with enough space.")
    print(f"Staging {len(pending)} files, {needed:,} bytes", flush=True)
    if source:
        def copy(p):
            dst = target / p
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_name(dst.name + ".copying")
            shutil.copyfile(source / p, tmp)
            os.replace(tmp, dst)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(copy, pending))
    elif pending:
        from huggingface_hub import snapshot_download
        snapshot_download(REPO, revision=REVISION, local_dir=str(target),
                          max_workers=4, allow_patterns=pending)
    if not verify(target, files):
        raise RuntimeError("Checkpoint size verification failed")
    receipt = {"repo": REPO, "revision": REVISION, "files": files}
    tmp = target / (RECEIPT + ".tmp")
    tmp.write_text(json.dumps(receipt, sort_keys=True))
    os.replace(tmp, target / RECEIPT)
    print(f"Verified {len(files)} files at {target}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("target", type=Path)
    parser.add_argument("--source", type=Path)
    args = parser.parse_args()
    stage(args.target, args.source)
