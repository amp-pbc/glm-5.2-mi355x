"""Render customer Kubernetes resources. Does not access a cluster."""
import argparse
import copy
from pathlib import Path
import sys
import yaml

ROOT = Path(__file__).resolve().parents[1]
BASE = "lmsysorg/sglang:v0.5.15.post1-rocm720-mi35x@sha256:40e940a0c55b87105c773d8b484616616b3a91662bfa223c48ff721d9793dc8d"
OVERLAY = "inferaimage/infera-overlay@sha256:6918eff34f201548a738dd592d2a1ece0627354d2e88f24a87cfa8f787a72a44"
LABEL = {"app.kubernetes.io/part-of": "glm52-recipe"}
MODEL = "GLM-5.2-MXFP4"
DOWNWARD = [{"name": key, "valueFrom": {"fieldRef": {"fieldPath": field}}}
            for key, field in [("POD_NAME", "metadata.name"), ("POD_NAMESPACE", "metadata.namespace"), ("POD_IP", "status.podIP")]]


def render(namespace="default", prefill=4, decode=4, policy="kv-aware"):
    def obj(kind, name, spec=None, **extra):
        api = "apps/v1" if kind == "Deployment" else "batch/v1" if kind == "Job" else "rbac.authorization.k8s.io/v1" if kind in ("Role", "RoleBinding") else "v1"
        out = {"apiVersion": api, "kind": kind,
               "metadata": {"name": name, "namespace": namespace, "labels": LABEL.copy()}, **extra}
        if spec is not None:
            out["spec"] = spec
        return out

    objects = [obj("ServiceAccount", "glm52-serving"),
               obj("Role", "glm52-serving", rules=[{"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "watch", "patch"]}]),
               obj("RoleBinding", "glm52-serving", roleRef={"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "glm52-serving"}, subjects=[{"kind": "ServiceAccount", "name": "glm52-serving", "namespace": namespace}]),
               obj("ConfigMap", "glm52-scripts", data={"stage.py": (ROOT / "scripts/stage.py").read_text()})]
    shared = {"name": "shared", "hostPath": {"path": "/mnt/shared", "type": "Directory"}}
    scripts = {"name": "scripts", "configMap": {"name": "glm52-scripts"}}
    stage_job = obj("Job", "glm52-weights", {
        "backoffLimit": 0, "activeDeadlineSeconds": 14400,
        "template": {"metadata": {"labels": LABEL.copy()}, "spec": {
            "restartPolicy": "Never", "nodeSelector": {"node-role.kubernetes.io/cpu": ""},
            "containers": [{"name": "download", "image": "python:3.12-slim", "command": ["bash", "-euc", "pip install --no-cache-dir huggingface-hub==1.32.0 && python /scripts/stage.py /shared/glm52/GLM-5.2-MXFP4"],
                "resources": {"requests": {"cpu": "2", "memory": "4Gi"}, "limits": {"memory": "16Gi"}},
                "volumeMounts": [{"name": "shared", "mountPath": "/shared"}, {"name": "scripts", "mountPath": "/scripts", "readOnly": True}]}],
            "volumes": [shared, scripts]}}})
    objects.append(stage_job)
    upstream = yaml.safe_load_all((ROOT / "upstream/disaggregated-kvd.yaml").read_text())
    deployment = next(d for d in upstream if d and d["kind"] == "InferaDeployment")
    components = deployment["spec"]["services"]
    for component in components.values():
        role = component.get("role", "router")
        is_router = role == "router"
        name = "glm52-" + role
        pod = copy.deepcopy(component["extraPodSpec"])
        labels = {**LABEL, "app.kubernetes.io/name": "glm52-router" if is_router else "glm52-worker", "glm52-role": role}
        pod["serviceAccountName"] = "glm52-serving"
        pod["nodeSelector"] = {"node-role.kubernetes.io/cpu" if is_router else "node-role.kubernetes.io/gpu-worker": ""}
        pod["terminationGracePeriodSeconds"] = 120
        main = pod["containers"][0]
        main["image"] = BASE
        main["env"] = copy.deepcopy(DOWNWARD) + [e for e in main.get("env", []) if not e["name"].startswith("POD_")]
        pod["initContainers"][0]["image"] = OVERLAY
        # The stock manifest's model PVC and per-role local-path PVCs cannot span nodes.
        pod["volumes"] = [{"name": "overlay", "emptyDir": {}}, shared, scripts]
        if is_router:
            main["command"] += ["--discovery-backend", "kubernetes", "--k8s-label-selector", "app.kubernetes.io/name=glm52-worker", "--router-backend", "python", "--router-policy", policy, "--kv-overlap-weight", "1.0"]
            main["command"][main["command"].index("--router-tokenizer-path") + 1] = f"/shared/glm52/{MODEL}"
            main["volumeMounts"] = [{"name": "overlay", "mountPath": "/overlay", "readOnly": True}, {"name": "shared", "mountPath": "/shared", "readOnly": True}]
            main["resources"] = {"requests": {"cpu": "8", "memory": "16Gi"}, "limits": {"memory": "32Gi"}}
            main["readinessProbe"] = {"httpGet": {"path": "/health", "port": 8000}, "timeoutSeconds": 5}
        else:
            pod["hostNetwork"] = True
            pod["dnsPolicy"] = "ClusterFirstWithHostNet"
            pod["tolerations"] = [{"key": "amd.com/gpu", "operator": "Equal", "value": "present", "effect": "NoSchedule"}]
            pod["affinity"] = {"podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [{"labelSelector": {"matchLabels": {"app.kubernetes.io/name": "glm52-worker"}}, "topologyKey": "kubernetes.io/hostname"}]}}
            main["command"] += ["--advertise-host", "$(POD_IP)", "--discovery-backend", "kubernetes", "--kv-events", "on", "--enable-cache-report", "--enable-metrics"]
            extra_env = {"SGLANG_OPT_USE_TILELANG_INDEXER": "1", "SGLANG_OPT_USE_TOPK_V2": "0", "SGLANG_OPT_USE_JIT_NORM": "0", "PYTHONHASHSEED": "0", "MC_GID_INDEX": "1", "MC_ENABLE_DEST_DEVICE_AFFINITY": "1", "MOONCAKE_DISABLE_HIP_DMABUF": "1", "INFERA_SGLANG_READY_TIMEOUT": "3600"}
            main["env"] += [{"name": k, "value": v} for k, v in extra_env.items()]
            main["env"] += [{"name": "SGLANG_HOST_IP", "value": "$(POD_IP)"}]
            main["securityContext"] = {"privileged": True, "capabilities": {"add": ["IPC_LOCK", "SYS_PTRACE"]}}
            main["resources"]["limits"].pop("memory", None)
            main["readinessProbe"] = {"httpGet": {"path": "/health", "port": 30000}, "timeoutSeconds": 10}
            main["volumeMounts"] = [{"name": "overlay", "mountPath": "/overlay", "readOnly": True}, {"name": "weights", "mountPath": "/models", "readOnly": True}, {"name": "kvd-socket", "mountPath": "/kvd"}, {"name": "shm", "mountPath": "/dev/shm"}, {"name": "infiniband", "mountPath": "/dev/infiniband"}, {"name": "host-libionic", "mountPath": "/host-libionic/libionic.so", "readOnly": True}]
            pod["volumes"] += [{"name": "weights", "hostPath": {"path": "/mnt/nvme/glm52/weights", "type": "DirectoryOrCreate"}}, {"name": "kvd-l3", "hostPath": {"path": f"/mnt/nvme/glm52/kvd/{role}", "type": "DirectoryOrCreate"}}, {"name": "kvd-socket", "emptyDir": {}}, {"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": "64Gi"}}, {"name": "infiniband", "hostPath": {"path": "/dev/infiniband", "type": "Directory"}}, {"name": "host-libionic", "hostPath": {"path": "/usr/lib/x86_64-linux-gnu/libionic.so.1", "type": "File"}}]
            pod["initContainers"].append({"name": "stage-weights", "image": "python:3.12-slim", "command": ["python", "/scripts/stage.py", f"/models/{MODEL}", "--source", f"/shared/glm52/{MODEL}"], "resources": {"requests": {"cpu": "2", "memory": "1Gi"}}, "volumeMounts": [{"name": "shared", "mountPath": "/shared", "readOnly": True}, {"name": "weights", "mountPath": "/models"}, {"name": "scripts", "mountPath": "/scripts", "readOnly": True}]})
            # Native Kubernetes sidecar: kvd must answer before engine startup.
            kvd = pod["containers"].pop(1)
            kvd["image"] = BASE
            kvd["volumeMounts"] = [{"name": "overlay", "mountPath": "/overlay", "readOnly": True}, {"name": "kvd-socket", "mountPath": "/kvd"}, {"name": "kvd-l3", "mountPath": "/l3"}]
            kvd["restartPolicy"] = "Always"
            kvd["startupProbe"] = {"exec": {"command": ["/overlay/bin/infera-exec", "python3", "-m", "infera.kvd.statctl", "--socket", "/kvd/kvd.sock"]}, "periodSeconds": 5, "timeoutSeconds": 10, "failureThreshold": 60}
            pod["initContainers"].append(kvd)
        objects.append(obj("Deployment", name, {"replicas": 1 if is_router else prefill if role == "prefill" else decode, "strategy": {"type": "Recreate"}, "progressDeadlineSeconds": 7200, "selector": {"matchLabels": labels}, "template": {"metadata": {"labels": labels}, "spec": pod}}))
    objects.append(obj("Service", "glm52-router", {"selector": {"app.kubernetes.io/name": "glm52-router"}, "ports": [{"name": "http", "port": 8000, "targetPort": 8000}]}))
    return objects


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--namespace", default="default")
    p.add_argument("--prefill", type=int, default=4)
    p.add_argument("--decode", type=int, default=4)
    p.add_argument("--policy", choices=["kv-aware", "round-robin"], default="kv-aware")
    p.add_argument("--phase", choices=["stage", "serve", "all"], default="all")
    a = p.parse_args()
    if a.prefill < 1 or a.decode < 1:
        p.error("Both roles need at least one worker")
    docs = render(a.namespace, a.prefill, a.decode, a.policy)
    if a.phase == "stage":
        docs = [d for d in docs if d["kind"] in ("ConfigMap", "Job")]
    elif a.phase == "serve":
        docs = [d for d in docs if d["kind"] != "Job"]
    yaml.safe_dump_all(docs, sys.stdout, sort_keys=False)
