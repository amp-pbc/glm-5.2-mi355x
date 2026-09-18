# Sources and licenses

This repository contains deployment code and documentation, not model weights, vendor images, or their bundled dependencies.

- AMD Infera recipe and adapter source audited at commit `625a950b109371aaf8ebedfdc05757b57ac32eab`: https://github.com/AMD-AGI/Infera/tree/625a950b109371aaf8ebedfdc05757b57ac32eab. The copied manifest and adapted deployment structure are MIT licensed, copyright 2026 Advanced Micro Devices, Inc. The original notice is retained in [licenses/Infera-LICENSE.txt](licenses/Infera-LICENSE.txt).
- Model: `amd/GLM-5.2-MXFP4`, revision `386bd0e4ec821f7b07975701cec3c3b953a5576a`: https://huggingface.co/amd/GLM-5.2-MXFP4/tree/386bd0e4ec821f7b07975701cec3c3b953a5576a. Its MIT license is copied verbatim into [licenses/GLM-5.2-MXFP4-LICENSE.txt](licenses/GLM-5.2-MXFP4-LICENSE.txt). The staging script also retains the upstream LICENSE with the downloaded checkpoint. The AMD model card identifies `zai-org/GLM-5.2` as its source model.
- SGLang vendor image: `lmsysorg/sglang:v0.5.15.post1-rocm720-mi35x@sha256:40e940a0c55b87105c773d8b484616616b3a91662bfa223c48ff721d9793dc8d`.
- Infera overlay image: `inferaimage/infera-overlay@sha256:6918eff34f201548a738dd592d2a1ece0627354d2e88f24a87cfa8f787a72a44`.

Image digests are copied from the audited upstream manifest. Local source review does not prove the image embeds that exact Infera commit. Capture runtime versions in the live validation receipt. Image contents retain their own component licenses; this repository's license does not replace them.
