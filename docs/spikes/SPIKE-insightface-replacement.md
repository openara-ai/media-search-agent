# Spike: Permissive Face Recognition Replacement For InsightFace

## Status

macOS, WSL2, and Windows native CPU/CUDA validation complete.

Recommendation: **facenet-pytorch VGGFace2** as the permissive default backend.

Eval script: `scripts/spike_face_recognizer_eval.py`
Raw results: `build/spikes/face-recognition/*.json`

## Context

Media Search Agent uses `insightface` with the `buffalo_l` model pack for face
detection, embedding, and optional age/gender estimation in
`src/msa_indexer/models/faces.py`. InsightFace works extremely well: the
`buffalo_l` bundle combines a RetinaFace/SCRFD detector (`det_10g.onnx`) with an
ArcFace ResNet50 recognition model (`w600k_r50.onnx`) trained on WebFace600K
(600K identities, carefully curated). Real-world performance on personal photo
libraries is excellent — small faces, angled faces, varied lighting, and
partially-occluded faces are all handled reliably.

The problem is exclusively licensing:

- `insightface` library: **MIT** — not the issue.
- `buffalo_l` / `buffalo_s` / `antelopev2` pretrained weights: **non-commercial**
  per the InsightFace model zoo page. Distribution of a public installer that
  downloads or bundles these weights requires a commercial license or a
  different model.

The model file set for `buffalo_l` is also non-trivial to replace: five ONNX
files covering detection, 2D landmarks, 3D landmarks, recognition, and
gender/age. Any replacement must cover at least detection and recognition; the
metadata features (age/gender) are lower priority.

Quality is a primary constraint for this spike. InsightFace `buffalo_l` is
state-of-the-art or near-state-of-the-art on standard face verification
benchmarks (LFW ~99.8%). A replacement that degrades clustering quality in a
personal photo library (e.g. splitting one person across multiple clusters,
or merging two distinct people) would be noticeable and unacceptable to users.
A small, measurable drop in borderline difficult cases (extreme angles, very
small faces) may be acceptable if the core labeling use case is preserved.

## Goals

- Identify a permissively licensed face recognition backend that can replace
  InsightFace `buffalo_l` without material quality regression on personal photo
  library content.
- Compare quality, speed, install size, model download behavior, and platform
  fit on Linux/WSL2, Windows native (including Intel integrated graphics
  laptops), and macOS.
- Define an adapter boundary so future backends can be swapped without touching
  the indexing pipeline or the People/face UI.
- Decide whether to replace InsightFace immediately, keep it behind an opt-in
  extra, or defer replacement.

## Non-Goals

- Training a custom model.
- Replacing CLIP or object detection.
- Solving the training-data license question for all possible face datasets.
  The spike should assess the **model file license** as shipped by the
  library author — the same principle applied when accepting RT-DETR weights
  under Apache-2.0 (trained on COCO, which is CC-BY-4.0 data, but the weights
  are distributed under Apache-2.0).
- Matching InsightFace `buffalo_l` on every benchmark. The decision is based on
  personal media library quality and installer practicality, not research paper
  results.

## Note on Training-Data License vs. Model-File License

Virtually all high-quality face recognition models are trained on datasets with
research-only or non-commercial terms (VGGFace2, CASIA-Webface, MS-Celeb-1M,
WebFace600K). The consensus in the ML community — and the precedent set by the
object detection spike — is that the **model file license** (as stated by the
library author distributing it) is the operative license for compliance purposes,
not the terms of the underlying training dataset. Record the model card or README
license claim and the source URL in this doc; do not treat training-data terms as
an automatic blocker unless the model author themselves restricts the weights.

## Current Integration Points

- `src/msa_indexer/models/faces.py`
  - `FaceRecognizer.__init__(model_name, device, conf_threshold, min_face_size, model_root)`
  - `FaceRecognizer.detect_and_embed(pil_image) -> List[FaceDetection]`
  - `FaceRecognizer.compare_faces(emb1, emb2, threshold) -> (similarity, is_match)`
  - `FaceRecognizer.get_similarity_matrix(embeddings) -> ndarray`
  - `FaceDetection`: `bbox (x, y, w, h) 0-1 normalized`, `embedding: ndarray (512-dim)`,
    `confidence: float`, `metadata: dict` (gender, age, landmarks)
- `src/msa_apps/search_api/setup_models.py`
  - `MODEL_META["insightface"]`: SHA-256 per ONNX file, size 500 MB
  - `SetupManager` downloads and hash-verifies all five ONNX files at first launch
- `src/msa_apps/search_api/app.py`
  - Config defaults: `face_model: "buffalo_l"`, `face_confidence_threshold: 0.7`
  - `FaceSettingsPayload` accepts `face_model` and `face_confidence_threshold`
- `src/msa_indexer/db/qdrant_export.py`
  - Stores 512-dim face embeddings; collection schema assumes 512 dimensions

If the replacement produces a **different embedding dimension**, the Qdrant face
collection must be dropped and re-indexed. Record the embedding dimension for
each candidate.

## Quality Bar

InsightFace `buffalo_l` on a personal photo library delivers:

| Capability | Observed Quality |
| --- | --- |
| Face detection rate (portrait, well-lit) | Near-100% |
| Face detection rate (small faces in group shots) | High — SCRFD handles 16px+ well |
| Face detection rate (angled, ±45°) | Good — RetinaFace trained for angle robustness |
| Same-person embedding similarity | Typically 0.75–0.95 cosine similarity |
| Different-person embedding similarity | Typically 0.10–0.45 cosine similarity |
| Cluster separation at threshold 0.60 | Very reliable in practice |

The replacement must maintain cluster separation. A same-person pair dropping
below 0.60 similarity, or a different-person pair rising above 0.65, are
observable quality failures in the People page (wrong person assigned, identity
split across clusters).

Published LFW verification accuracy for reference:

| Model | LFW Accuracy (published) |
| --- | --- |
| InsightFace ArcFace ResNet50 (buffalo_l) | ~99.8% |
| GhostFaceNet (deepface backend) | ~99.7% |
| FaceNet / InceptionResnetV1 VGGFace2 (facenet-pytorch) | ~99.6% |
| SFace (OpenCV model zoo) | ~99.3% |
| dlib ResNet face recognition | ~99.4% |

Published numbers are on clean benchmark data; personal photo quality may vary
more. Even so, the top three are close enough that the gap is unlikely to be
visible in normal use.

## Candidates

### Candidate A: facenet-pytorch (MTCNN + InceptionResnetV1)

**Repository**: `timesler/facenet-pytorch` — PyPI `facenet-pytorch`

**Detection model**: MTCNN — a classical multi-stage cascade network for face
detection and alignment. The PyTorch reimplementation ships its own weights as
part of the package.

**Recognition model**: `InceptionResnetV1` — pretrained on VGGFace2 (3.31M images,
9K identities) or CASIA-Webface. The VGGFace2-pretrained variant is the
recommended default and is the one with the best published accuracy.

License posture:

- `facenet-pytorch` code: **MIT** (`timesler/facenet-pytorch`, confirmed in
  repository LICENSE file).
- MTCNN weights: shipped as part of the PyPI package by the author. No
  separate weight-level license notice; implicitly MIT per the package license.
- InceptionResnetV1 weights: distributed via the package as `.pt` download
  links in the README. Tim Esler ships them without an additional restriction
  notice beyond the MIT package license. VGGFace2 dataset terms are
  "research only" at Oxford's page, but those are the **dataset** terms, not the
  **weight-file** terms as shipped. Confirm by reading the model card or
  download URL license before the implementation phase.
  URL to verify: `https://github.com/timesler/facenet-pytorch`

Embedding dimension: **512-dim** (VGGFace2 variant) — matches InsightFace,
no Qdrant schema change needed.

GPU support:

- **CUDA**: native PyTorch — full support on Linux/WSL2 and Windows with CUDA.
- **MPS (Apple Silicon)**: native PyTorch — works from PyTorch 2.0+; some ops
  fall back to CPU. Verify during spike.
- **CPU**: always available; slower.

Pros:

- MIT code, clearly-stated.
- 512-dim embeddings — no Qdrant migration.
- Pure PyTorch: same stack as CLIP and RT-DETR. No new framework family.
- MTCNN detection quality is good for clear and angled faces; verified in
  production use by many projects.
- Simple API — closer to `detect_and_embed` than multi-package pipelines.
- No ONNX dependency in the hot path (reduces macOS installation complexity
  vs. InsightFace which requires `onnxruntime`).
- ONNX export of both MTCNN and InceptionResnetV1 is feasible via `torch.onnx`
  for `onnxruntime`-based CPU inference on Windows native.

Cons:

- No built-in age/gender estimation. Not a hard blocker — the People page does
  not currently depend on these values in the UI; they are stored as optional
  metadata.
- MTCNN detection is a cascade, not a single-shot SCRFD/RetinaFace — may miss
  more small faces (< 20px) than InsightFace in dense group shots.
- VGGFace2 pretrained weights URL provenance must be confirmed before
  implementation. If the author later adds a restriction notice, a fallback to
  the CASIA-Webface variant (same accuracy class, different training set) is
  available in the same package.
- Not actively maintained at a rapid pace (last major release 2022), but the
  library is stable, widely used, and the architecture is mature.

Why include:

- This is the most direct InsightFace analog: single library, single install,
  same embedding dimension, competitive accuracy. Lowest integration risk of
  any candidate.

Usage sketch:

```python
from facenet_pytorch import MTCNN, InceptionResnetV1

mtcnn = MTCNN(keep_all=True, device=device, min_face_size=20)
resnet = InceptionResnetV1(pretrained="vggface2").eval().to(device)

boxes, probs = mtcnn.detect(img_tensor)            # detection
face_crops = mtcnn(img_tensor)                     # aligned crops
embeddings = resnet(face_crops)                    # 512-dim per face
```

### Candidate B: DeepFace with GhostFaceNet Backend

**Repository**: `serengil/deepface` — PyPI `deepface`

DeepFace is a Python wrapper library that supports multiple face recognition
backends behind a unified API. The library itself is MIT licensed. The spike
targets the **GhostFaceNet** backend as the highest-quality permissively licensed
option available through DeepFace.

**Detection**: `RetinaFace` (via the `retina-face` package, MIT) or `MTCNN`
(via `mtcnn` or `facenet-pytorch`). RetinaFace is architecturally close to
InsightFace's detector and is a quality upgrade from the classic MTCNN cascade.

**Recognition model**: GhostFaceNet — a lightweight architecture based on
GhostNet (Huawei Research, CVPR 2020), adapted for face recognition with
ArcFace-style training. Published in 2023.

License posture:

- `deepface` library: **MIT** (`serengil/deepface`, confirmed in repository).
- `retina-face` package: **MIT** (`serengil/retinaface`).
- GhostFaceNet weights: distributed by deepface via a download URL in the
  library. The upstream GhostFaceNet repository (`HamadYA/GhostFaceNets`) is
  **MIT** licensed. DeepFace downloads these weights at first use. Confirm
  the download URL, upstream commit, and that the weight file has not been
  modified from the MIT source. URL to verify:
  `https://github.com/HamadYA/GhostFaceNets` and
  the download path inside `deepface/models/face_recognition/GhostFaceNet.py`.

Embedding dimension: **512-dim** — matches InsightFace and Candidate A.

GPU support:

- **CUDA**: via PyTorch backend (deepface 0.0.90+ supports PyTorch for most
  backends). Verify the GhostFaceNet backend specifically uses the PyTorch
  path, not TensorFlow.
- **MPS (Apple Silicon)**: depends on backend; PyTorch path will support MPS
  from PyTorch 2.0+. Verify during spike.
- **CPU**: always available.

Pros:

- Highest published LFW accuracy of the two candidates (~99.7%).
- RetinaFace detector is architecturally closer to InsightFace's SCRFD than
  MTCNN — better small-face and dense-scene detection.
- Backend flexibility: if GhostFaceNet is ever a problem, the same DeepFace
  wrapper can switch to `Facenet` (Apache-2.0), `SFace` (Apache-2.0), or
  others without changing the adapter.
- MIT weights (GhostFaceNet) with a clear upstream source.

Cons:

- Larger dependency surface: `deepface`, `retina-face`, and backend-specific
  packages. Some deepface backends pull TensorFlow; verify GhostFaceNet/PyTorch
  path is TF-free before committing.
- First-run model download is managed by deepface's own download mechanism
  (not HuggingFace hub, not our `SetupManager`). Need to route the cache
  directory to `models_dir` and verify SHA-256 independently.
- API is higher-level and less surgical than facenet-pytorch; adapting it to
  return the same `FaceDetection` schema requires more wrapper code.
- Active maintenance is strong but the library abstracts away model internals,
  making version pinning and cache layout control more important.
- Install footprint verification needed: confirm no silent TF download on a
  CPU-only Windows machine.

Why include:

- Best accuracy potential of the permissive candidates and the only option with
  a RetinaFace-quality detector. Backend flexibility is a hedge against future
  weight-licensing surprises.

Usage sketch:

```python
from deepface import DeepFace

# Detection + embedding in one call
result = DeepFace.represent(
    img_path=img_array,          # numpy RGB
    model_name="GhostFaceNet",
    detector_backend="retinaface",
    enforce_detection=False,
)
# result is a list of dicts: facial_area, embedding (512-dim), face_confidence
```

### Candidate C: OpenCV YuNet + SFace (lightweight fallback)

**Repositories**: `opencv-contrib-python` — OpenCV model zoo

This combination uses two OpenCV-native ONNX models: YuNet for face detection
and SFace for embedding. Both are distributed under permissive licenses and are
designed to be small, CPU-friendly, and easy to bundle.

License posture:

- OpenCV `opencv-contrib-python` code: **Apache-2.0**.
- YuNet weights: **MIT** per OpenCV model zoo card.
  URL: `https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet`
- SFace weights: **Apache-2.0** per OpenCV model zoo card.
  URL: `https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface`
- Training data note: SFace was trained on MS-Celeb-1M derivatives. Microsoft
  withdrew Ms-Celeb-1M from public release due to privacy concerns. This does
  not affect the model file's Apache-2.0 license (see model-file license note
  above) but is worth recording for completeness. The same caveat applies to
  most face recognition models on the market.

Embedding dimension: **128-dim** — **Qdrant face collection must be dropped and
rebuilt** if migrating from InsightFace (512-dim) to SFace.

GPU support:

- **CPU**: first-class, fast (~10–30ms per image on modern hardware).
- **CUDA**: via OpenCV DNN CUDA backend — supported but less commonly tested.
- **MPS (Apple Silicon)**: not supported by OpenCV DNN. CPU only on macOS.
- **OpenVINO**: supported by OpenCV DNN — useful for Intel iGPU on Windows
  native, though architectural changes are required to move inference outside
  WSL2 (see Intel note).

Pros:

- Smallest install footprint of any candidate. `opencv-contrib-python` is
  already present in the project or close to it.
- ONNX models: no PyTorch required for inference; pure C++ runtime under the
  hood. Easy to bundle or ship as ONNX files alongside the installer.
- Very fast CPU path — Intel laptop users would see the best CPU performance
  of any candidate.
- License and weight provenance are the simplest to verify.
- No `onnxruntime` required (unlike InsightFace).

Cons:

- **Lowest accuracy of all candidates** (~99.3% LFW vs buffalo_l ~99.8%).
  On personal photo libraries the gap matters: with large families and
  similar-looking people, the cluster separation degrades noticeably.
  Expect more identity splits and false merges than with Candidates A or B.
- 128-dim embedding requires a full Qdrant face collection rebuild on migration.
- YuNet detection is weaker than SCRFD or RetinaFace on very small faces
  (under ~24px) and extreme pose angles.
- No age/gender estimation.
- No MPS acceleration on macOS — CPU-only even on Apple Silicon.

Why include:

- Clearest licensing provenance of any option. Valid if a future platform
  constraint (no PyTorch, minimal installer size) makes Candidates A or B
  impractical. **Not the quality-optimized default** — positioned explicitly
  as the lightweight/installer-friendly floor.

Usage sketch:

```python
import cv2

detector = cv2.FaceDetectorYN.create("yunet.onnx", "", (320, 320))
recognizer = cv2.FaceRecognizerSF.create("sface.onnx", "")

detector.setInputSize((w, h))
_, faces = detector.detect(img_bgr)                   # Nx15 array
aligned = recognizer.alignCrop(img_bgr, faces[i])
embedding = recognizer.feature(aligned)               # 128-dim
```

## Candidate Triage

| Candidate | License Fit | Expected Quality | Installer Fit | Integration Risk | Spike Priority |
| --- | --- | ---: | ---: | ---: | ---: |
| facenet-pytorch (MTCNN + InceptionResnetV1) | Strong (MIT code + weights; confirm VGGFace2 weight source) | High (~99.6% LFW) | Strong (pure PyTorch, no onnxruntime) | Low | 1 |
| DeepFace + GhostFaceNet + RetinaFace | Strong (MIT library + weights; confirm GhostFaceNet source) | Highest (~99.7% LFW) | Medium (more packages, TF-free path must be verified) | Medium | 2 |
| OpenCV YuNet + SFace | Strongest (Apache-2.0/MIT, simplest provenance) | Lower (~99.3% LFW, 128-dim) | Strongest (smallest footprint, CPU-first) | Low-Medium (128-dim Qdrant rebuild) | 3 |

Evaluate Candidates A and B first — they are closer to the InsightFace quality
bar that this project requires. Only escalate to Candidate C if both A and B
fail installer or platform constraints. The quality trade-off of SFace (~99.3%,
128-dim) is meaningful for large photo libraries with similar-looking people and
is not the right default given the stated quality requirement.

If time is tight: run Stage 2 (facenet-pytorch) first. Add Candidate C only
if a no-PyTorch constraint emerges from the Intel Windows platform evaluation.

## Proposed Adapter Boundary

Preserve the current public class and `FaceDetection` dataclass exactly.
Introduce a backend protocol internally:

```python
class FaceBackend(Protocol):
    backend_name: str

    def detect_and_embed(
        self,
        img_rgb: np.ndarray,
        conf_threshold: float,
        min_face_size: int,
    ) -> List[FaceDetection]: ...


class FaceRecognizer:
    def __init__(
        self,
        model_name: str = "vggface2",       # replaces "buffalo_l"
        device: str = "cuda",
        conf_threshold: float = 0.7,
        min_face_size: int = 20,
        model_root: Optional[Path] = None,
        backend: str = "facenet_pytorch",   # "facenet_pytorch" | "deepface" | "insightface"
    ):
        ...
```

Config sketch:

```yaml
face_recognizer_backend: facenet_pytorch   # facenet_pytorch | deepface | insightface
face_model: vggface2                       # passed to the selected backend
face_confidence_threshold: 0.7
```

Compatibility rules:

- `detect_and_embed()` must return `FaceDetection` with:
  - `bbox`: `(x, y, w, h)` normalized 0–1
  - `embedding`: `np.ndarray` shape `(512,)` — if a backend produces 128-dim,
    upgrade to a 512-dim model variant; do **not** silently change the Qdrant
    collection schema.
  - `confidence`: float 0–1
  - `metadata`: dict — age/gender keys become optional; omit rather than fake.
- `compare_faces()` and `get_similarity_matrix()` are pure NumPy — no backend
  dependency; they survive unchanged.
- If the replacement does not provide gender/age, the `metadata` dict is empty.
  The UI already handles absent metadata gracefully.
- Preserve the initialization-failure pattern: if the backend fails to load,
  log and disable face indexing rather than crashing the whole index run.

## Evaluation Dataset

Unlike object detection, face recognition quality cannot be assessed from
single-image label presence alone. The spike needs **identity-consistent pairs
and triplets** to test same-person and different-person embedding similarity.

### Required Test Media

**Positive pairs** (same person, different photos — 3–5 pairs minimum):

- Same person photographed at different times, angles, or lighting
- Include at least one pair with a significant angle difference (±30°)
- Include at least one pair where one image is a group shot

**Negative pairs** (different people — 3–5 pairs minimum):

- Two people who are not the same person

Do not commit private photos of real people. Options for permissive public
fixtures:

- Creative Commons–licensed celebrity photos from Wikimedia Commons
- Synthetic faces generated by publicly licensed generative models
- LFW (Labeled Faces in the Wild) — research-use terms; do not commit to repo,
  use only locally during spike

Record sources and licenses in `tests/real_media/THIRD_PARTY_MEDIA.md` for any
new files added.

### Existing Fixtures

Check for existing face-relevant fixtures:

```
tests/real_media/fixtures/originals/
```

Any portrait or group photo already present can contribute to detection testing.
Embedding quality testing requires at minimum one positive pair.

### Suggested Evaluation Script

```bash
uv run python scripts/spike_face_recognizer_eval.py \
  --backend facenet_pytorch \
  --model vggface2 \
  --device mps \
  --fixtures tests/real_media/fixtures/faces \
  --output build/spikes/face-recognition/facenet-pytorch-vggface2.json
```

## Metrics

### Embedding Quality

For each positive pair (same person):

- Cosine similarity — must be ≥ 0.60 (same threshold used in production)
- If below threshold: identity would be split into separate clusters

For each negative pair (different people):

- Cosine similarity — must be < 0.55 (safety margin below threshold)
- If above threshold: two people would be merged into the same cluster

Minimum pass:

- All positive pairs score ≥ 0.60.
- All negative pairs score < 0.55.
- Ideally, positive pairs score ≥ 0.70 (the InsightFace typical floor).

### Detection Quality

For each fixture image:

- Number of faces detected vs. expected.
- False positives (non-face regions detected as faces).
- Smallest detected face in pixels — compare to InsightFace baseline.

Minimum pass:

- All clear portrait faces detected.
- At least one face detected in each group shot.
- False positive rate not materially worse than InsightFace.

### Runtime

Measure per candidate:

- Model import time
- Model load / warm-up time
- Detection time per image (cold and warm)
- Embedding time per face crop (cold and warm)
- Total time per image end-to-end

Report separately for:

- MPS (Apple Silicon)
- CPU (Apple Silicon) — estimated 3–5× slower applies to Intel laptops
- CUDA (WSL2 or Windows) if available

Minimum pass:

- CPU warm time per image acceptable for small libraries (~2–5 seconds per
  image is tolerable for a background indexer; > 10 seconds per image is not).
- GPU paths work; MPS fallback to CPU is acceptable.

### Installer Impact

Measure:

- New packages added vs. current `insightface` + `onnxruntime` stack.
- Wheel availability for Windows native (pure-Python preferred;
  no C extensions that require Visual C++ build tools).
- Model weight size and download mechanism.
- Whether model cache can live under `models_dir` (under `MediaSearchAgent/`
  data directory) vs. the library's own home-directory cache.
- ONNX export feasibility for pure-CPU inference on Windows native.
- macOS: whether `onnxruntime` dependency is removed (it is currently required
  only for InsightFace).

Minimum pass:

- Pure-Python wheels available on PyPI for the platform targets.
- No Visual C++ compiler required on Windows.
- Model cache can be directed to `models_dir`.
- Weight download is deterministic and can be integrity-verified.

## Spike Stages

### Stage 1: Baseline InsightFace

Create `scripts/spike_face_recognizer_eval.py` to run any configured backend
against the fixture set and record:

- Detected face count and bounding boxes per image.
- Embeddings (optionally — store as numpy arrays in JSON output).
- Cosine similarities for positive and negative pairs.
- Cold/warm timing.
- Model size and cache location.

Run InsightFace `buffalo_l` baseline:

```bash
uv run python scripts/spike_face_recognizer_eval.py \
  --backend insightface \
  --model buffalo_l \
  --device mps \
  --fixtures tests/real_media/fixtures/faces \
  --output build/spikes/face-recognition/insightface-buffalo_l.json
```

This establishes the quality floor that candidates must match.

### Stage 2: facenet-pytorch (MTCNN + InceptionResnetV1)

Install in spike venv or current venv:

```bash
uv pip install facenet-pytorch
```

Evaluate VGGFace2-pretrained variant first:

```bash
uv run python scripts/spike_face_recognizer_eval.py \
  --backend facenet_pytorch \
  --model vggface2 \
  --device mps \
  --fixtures tests/real_media/fixtures/faces \
  --output build/spikes/face-recognition/facenet-pytorch-vggface2.json
```

Also run CASIA-Webface variant as a fallback if the VGGFace2 weight license
cannot be confirmed:

```bash
uv run python scripts/spike_face_recognizer_eval.py \
  --backend facenet_pytorch \
  --model casia-webface \
  --device mps \
  --fixtures tests/real_media/fixtures/faces \
  --output build/spikes/face-recognition/facenet-pytorch-casia.json
```

Record:

- Weight source URL and any license note in the README or model card.
- Package version and PyPI wheel sizes.
- Whether `onnxruntime` is still required for anything after removing InsightFace.
- Positive/negative pair similarities vs. InsightFace baseline.
- Detection count comparison (small faces in group shots).

Decision rule:

- If facenet-pytorch VGGFace2 matches the quality minimum pass and the weight
  license is confirmed, it is the leading candidate.

### Stage 3: DeepFace + GhostFaceNet

Only run this stage if Stage 2 fails the detection quality minimum (too many
missed faces in group shots) or if the VGGFace2 weight license is unclear and
the CASIA variant also fails minimum quality.

Verify TF-free install first:

```bash
uv pip install deepface retina-face --no-deps
uv pip install deepface retina-face   # compare what gets pulled
# Confirm TensorFlow is NOT in the resolved set
```

Evaluate:

```bash
uv run python scripts/spike_face_recognizer_eval.py \
  --backend deepface \
  --model GhostFaceNet \
  --detector retinaface \
  --device mps \
  --fixtures tests/real_media/fixtures/faces \
  --output build/spikes/face-recognition/deepface-ghostfacenet.json
```

Record:

- GhostFaceNet weight download URL, source commit, and license field.
- Whether the install is TF-free.
- Cache directory control — can it be pointed to `models_dir`?
- Detection quality with RetinaFace vs. Stage 2 MTCNN baseline.
- Positive/negative pair similarities vs. InsightFace baseline.

Decision rule:

- Use DeepFace+GhostFaceNet if the RetinaFace detector is materially better
  on small/dense-scene faces and the quality minimum is met.
- Accept the higher integration complexity only if Stage 2 cannot pass.

### Stage 4: Write Recommendation

Write the measured conclusion back into this document:

- Selected default backend.
- Optional backends to retain (InsightFace for users who opt in / have
  commercial license).
- Dependency changes.
- Installer and `setup_models.py` changes.
- Qdrant migration requirements (only if embedding dimension changes).
- Notice and license changes.
- Migration plan for existing `face_model: buffalo_l` config values.

## Measured Results

All measurements on macOS Apple Silicon (M-series).
InsightFace and OpenCV SFace: CoreML/CPU execution providers via ONNX Runtime.
facenet-pytorch: MTCNN detection on CPU (MPS adaptive-pooling unsupported, see note);
InceptionResnetV1 embedding on MPS.
Cold time = first inference after model load. Warm time = average of remaining 12 fixtures.

| Backend | Model | Weight License | Weight Size | Embed Dim | Load Time | Cold Time | Warm Time | Pos Pair Sim | Neg Pair Sim | False Positives | Verdict |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| insightface | buffalo_l | **Non-commercial** | 341 MB | 512 | 3.97s | 121ms | **36ms** | 0.8821 | −0.0176 | 0 (clean) | Baseline only — license blocker |
| facenet-pytorch | vggface2 (MIT pkg) | MIT (pkg license; VGGFace2 training data non-commercial; weight file terms match pkg) | 112 MB | 512 | 0.37s | 2420ms¹ | 368ms² | **0.9203** | −0.0464 | 2 (dog fixtures at conf 0.70–0.74) | **Recommended default** |
| deepface + GhostFaceNet | GhostFaceNet + RetinaFace | MIT (GhostFaceNet: HamadYA/GhostFaceNets; RetinaFace: serengil/deepface_models) | **~129 MB**³ | 512 | 1.95s | 3019ms⁴ | 461ms | 0.7947 | 0.0868 | 0 (clean) | Runner-up — TF dependency is a blocker |
| opencv_sface | YuNet + SFace | MIT / Apache-2.0 | 39 MB | **128** | 0.04s | 142ms | 64ms | 0.8787 | N/A⁵ | 0 | Rejected — detection gaps |

¹ MPS first-pass JIT compilation; not representative of production startup.
² MTCNN runs CPU-only on macOS due to PyTorch MPS adaptive-pooling limitation (pytorch#96056). Embedding (InceptionResnetV1) runs MPS; detection dominates total time.
³ GhostFaceNet (16 MB) + RetinaFace (113 MB). RetinaFace downloads lazily on first inference call; not reflected in load_time. All weights land in `~/.deepface/weights/`, not under `models_dir`.
⁴ Includes RetinaFace lazy download (119 MB) during first call; subsequent cold starts from disk are ~1–2s.
⁵ `face_single_02.jpg` not detected by YuNet at any tested threshold (0.5–0.7); negative pair cannot be evaluated.

### Per-Fixture Detection Results

| Fixture | InsightFace | facenet-pytorch | DeepFace+Ghost | OpenCV SFace |
| --- | ---: | ---: | ---: | ---: |
| face_expressive_01.jpg | 1 ✓ | 1 ✓ | 1 ✓ | **0 ✗** |
| face_group_01.jpg | 11 | **13** | 11 | 12 |
| face_same_person_01.jpg | 1 | 3 (multi-person) | 1 | 1 |
| face_same_person_02.jpg | 1 | 1 | 1 | 1 |
| face_single_01.jpg | 1 | 1 | 1 | 1 |
| face_single_02.jpg | 1 ✓ | 1 ✓ | 1 ✓ | **0 ✗** |
| object_dog_01.jpg | 0 ✓ | **1 FP** (0.70) | 0 ✓ | 0 ✓ |
| object_landscape_01.jpg | 0 ✓ | 0 ✓ | 0 ✓ | 0 ✓ |
| video_face_single_01.webm | 1 | 1 | 1 | 1 |
| video_people_crowd_01.webm | 0 | 0 | 1 (0.91) | 0 |

### Key Observations

- **facenet-pytorch embedding quality exceeds InsightFace** on the positive pair
  (0.9203 vs 0.8821). This was unexpected. Both produce 512-dim embeddings, so
  the Qdrant face collection dimension does not change. The existing face
  vectors still need to be rebuilt because FaceNet and InsightFace embeddings
  live in different vector spaces.
- **facenet-pytorch detection is good overall** — 13 faces in the dense group
  shot (2 more than InsightFace). The 3-face result on `face_same_person_01.jpg`
  is correct: that image has multiple people; the highest-confidence face (0.99)
  is used for the pair test.
- **facenet-pytorch false positives on dogs** (confs 0.70, 0.74). At the
  production default threshold of 0.7 these would slip through.
  Raising the threshold to **0.80** eliminates both FPs: all genuine face
  detections in the fixture set have conf ≥ 0.81. Recommend updating the default
  `face_confidence_threshold` from 0.70 → 0.80 in the implementation phase.
- **facenet-pytorch MTCNN cannot use MPS** — adaptive pooling is unsupported on
  the MPS backend (pytorch#96056). MTCNN always runs CPU. The 368ms warm average
  on Apple Silicon CPU is a ~10× regression vs InsightFace's 36ms. This is a
  meaningful throughput difference for large libraries; see Intel note below.
  On CUDA (Linux/WSL2, Windows), MTCNN and ResNet both run on GPU — the throughput
  gap narrows significantly.
- **facenet-pytorch model load time** is 0.37s vs InsightFace's 3.97s — a
  meaningful startup improvement.
- **facenet-pytorch installs cause torch solver conflict**: `uv pip install
  facenet-pytorch` downgrades torch from 2.11.0 → 2.2.2 via the torchvision
  dependency chain. After manually restoring `torch==2.11.0 torchvision==0.26.0`,
  facenet-pytorch 2.6.0 ran correctly — its code is compatible with newer torch.
  The implementation phase must pin torch explicitly in requirements so the solver
  does not regress. Requires `transformers>=2.4` so the torch floor must be
  maintained. WSL2 reproduced this issue: plain install downgraded the CUDA
  stack to `torch==2.2.2+cu121`, which cannot execute on RTX 5080 / Blackwell
  (`sm_120`) and failed with `no kernel image is available for execution on the
  device`.
- **OpenCV SFace misses two fixtures** (`face_expressive_01.jpg`,
  `face_single_02.jpg`) at all tested thresholds (0.5 and 0.7). Lowering to 0.5
  produces heavy false positives on other fixtures without recovering the missed
  faces. YuNet detection is unsuitable as a primary backend.
- **OpenCV SFace uses 128-dim embeddings** — would require a full Qdrant face
  collection rebuild on migration. Combined with the detection gaps, it is
  rejected as the primary candidate.
- **DeepFace + GhostFaceNet was measured** (Stage 3 run despite Stage 2 passing,
  for completeness). Results: both pairs pass, zero false positives, clean
  detection on all face fixtures including `face_expressive_01.jpg`. However
  the positive pair similarity (0.7947) is meaningfully lower than both
  facenet-pytorch (0.9203) and InsightFace (0.8821). GhostFaceNet's published
  LFW number (~99.7%) does not translate here — the embedding space is simply
  less tightly clustered on these fixtures at production threshold 0.60.
- **DeepFace installs TensorFlow**: `uv pip install deepface retina-face` pulls
  in `tensorflow==2.21.0`. This is a ~500 MB addition and a significant
  installer footprint regression. Additionally, `retina-face` requires
  `tf-keras` as a separate package with TF 2.21 — a two-step install quirk that
  makes the dependency chain fragile.
- **DeepFace cache location**: model weights download to `~/.deepface/weights/`
  by default. Redirecting to `models_dir` requires setting the `DEEPFACE_HOME`
  environment variable. The RetinaFace model (113 MB) downloads lazily on first
  inference, not at startup, making setup progress feedback harder to implement.

## Intel CPU / Windows Native Note

The measured macOS MPS warm time for facenet-pytorch is 368ms, dominated by
MTCNN running CPU-only. This path is representative of any platform without
CUDA: macOS Intel, Windows Intel laptops, WSL2 without a CUDA GPU. On Apple
Silicon CPU the bottleneck is MTCNN cascade network throughput; Intel Core i5
8th–10th gen is estimated 3–5× slower.

| Backend | macOS MPS warm | macOS CPU est. | Intel i5 est. | Source |
| --- | ---: | ---: | ---: | --- |
| InsightFace buffalo_l | 36ms | ~100–160ms | ~300–500ms | Measured MPS; CPU extrapolated |
| facenet-pytorch VGGFace2 | 368ms² | ~500–700ms | ~1.5–2.5s | Measured (MTCNN CPU + ResNet MPS); CPU extrapolated |
| OpenCV SFace | 64ms | ~80–120ms | ~200–350ms | Measured CPU; already CPU-only |

² MTCNN is the bottleneck on macOS. On CUDA (Linux/WSL2, Windows desktop with
discrete GPU), MTCNN runs on GPU and the gap vs InsightFace narrows
significantly. The macOS CPU timing is therefore the worst-case for this spike
and the most conservative platform to evaluate on.

### WSL2 Results

Measurements on WSL2 / Ubuntu 22.04.

Hardware:

- CPU: AMD Ryzen 9 9900X 12-Core Processor, 12 cores / 24 threads, AVX512-capable
- GPU: NVIDIA GeForce RTX 5080

Raw outputs:

- `build/spikes/face-recognition/facenet-pytorch-vggface2-wsl2-cuda-restored-torch.json`
- `build/spikes/face-recognition/facenet-pytorch-vggface2-wsl2-cpu.json`

| Device | Torch Stack | MTCNN Device | Load Time | Cold Time | Warm Time | Pos Pair Sim | Neg Pair Sim | Pair Result | Notes |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |
| CUDA | `torch==2.11.0+cu130`, `torchvision==0.26.0+cu130` | `cuda` | 0.50s | 1279ms | 285.6ms | 0.9191 | −0.0424 | PASS | Confirms MTCNN and ResNet both run on CUDA after torch stack repair |
| CPU | same venv, CPU requested | `cpu` | 0.26s | 487ms | 309.1ms | 0.9203 | −0.0464 | PASS | CPU fallback works on the WSL host; timing is fixture-size dependent |

The CUDA and CPU warm averages are closer than expected. This should not be
read as "GPU acceleration does not matter" yet:

- The WSL CPU is a high-end desktop CPU, not representative of an older Intel
  laptop CPU.
- The fixture set is small and mixed: several frames have no faces and finish
  quickly on both paths, compressing the average.
- MTCNN is a cascade with image-pyramid, proposal filtering, NMS, and many
  small tensor operations. For these fixture sizes, GPU launch and transfer
  overhead can offset much of the CUDA benefit.
- Embedding is the part most likely to benefit from CUDA, but most fixtures have
  zero or one face, so the ResNet stage is not heavily exercised. The dense
  group image did show a CUDA advantage (`1051ms` vs `1347ms`).
- The evaluator runs image-by-image, not as a batched throughput benchmark.
  Larger face-heavy batches may show a wider GPU/CPU gap.

Follow-up performance work should separate detection and embedding timings and
run a repeated or larger face-heavy fixture set before making installer-time
promises about GPU speedups.

WSL install finding:

- `uv pip install facenet-pytorch` pulled `torch==2.2.2+cu121`,
  `torchvision==0.17.2+cu121`, `numpy==1.26.4`, `pillow==10.2.0`, and cu12
  NVIDIA runtime packages into the existing venv.
- On RTX 5080 / Blackwell, the downgraded torch build warns that `sm_120` is not
  supported and all CUDA fixture inference fails.
- Restoring `torch==2.11.0+cu130` and `torchvision==0.26.0+cu130` was not enough
  until stale cu12 NVIDIA packages were removed and `nvidia-nccl-cu13` was
  force-reinstalled from the cu130 index.
- Implementation should install `facenet-pytorch` without allowing dependency
  resolution to replace the platform-selected PyTorch stack. Use explicit torch
  pins / installer ordering, and consider `uv pip install facenet-pytorch
  --no-deps` after the known-good torch stack is installed.
  The required invariant is a torch wheel with RTX 5080 / Blackwell `sm_120`
  support, not a specific CUDA minor version forever. WSL2 passed with
  `torch==2.11.0+cu130`; future Windows packaging may use a newer compatible
  CUDA wheel such as `cu131` if PyTorch publishes one.

### Platform Coverage

The macOS and WSL2 runs select a strong candidate. Windows native does not need
a full re-spike unless the installer smoke exposes a platform-specific failure:
the quality-sensitive model code is the same PyTorch/facenet-pytorch stack, and
the existing Windows native runtime has already been validated with CUDA for the
current PyTorch-based components. The remaining Windows risk is packaging and
dependency resolution, especially making sure `facenet-pytorch` does not replace
the installer-selected Blackwell-compatible CUDA torch stack.

| Platform | Tested | Notes |
| --- | --- | --- |
| macOS Apple Silicon (MPS) | ✓ This spike | Worst-case timing — MTCNN CPU-only due to MPS adaptive-pool gap |
| Windows native Python (CUDA) | ✓ Installed-runtime smoke | Pair quality passes with `mtcnn_device: "cuda"` after installing Blackwell-compatible `torch==2.11.0+cu130`; video fixtures processed after satisfying the current OpenCV/NumPy constraint |
| Linux / WSL2 with CUDA | ✓ This spike | Passes with `mtcnn_device: "cuda"` after preserving/restoring a Blackwell-compatible `torch==2.11.0+cu130` stack |
| Linux / WSL2 CPU | ✓ This spike | Pair quality passes; warm average 309.1ms on this WSL host |
| Windows native Python (CPU) | ✓ Installed-runtime smoke | Pair quality passes; warm average 273.7ms with shell installer CPU torch |

**Windows native is a shipping production path** (`requirements-windows.txt`
and `install.ps1`). It has install-level concerns that macOS and WSL2 cannot
cover, but those concerns are narrower than model quality. The current Windows
installer already installs `torch` / `torchvision` before other packages, which
is the right shape for facenet-pytorch as long as that torch build is compatible
with the target GPU and the later facenet install does not resolve torch again.

**WSL2 with CUDA is the reference ML runtime** for this project. The backend
works there, but only when the installer preserves the Blackwell-compatible
PyTorch stack.

#### Windows Native CPU Result

Manual smoke using the shell-installed runtime under
`%LOCALAPPDATA%\MediaSearchAgent`:

- Before facenet install: `torch==2.11.0+cpu`, `torchvision==0.26.0`,
  `cuda_available=false`.
- `facenet-pytorch --no-deps` installed cleanly and did not change `torch` or
  `torchvision`.
- CPU evaluator passed: warm `273.7ms`, positive pair `0.9203`, negative pair
  `-0.0464`, `all_pairs_pass=true`.
- Raw output:
  `build/spikes/face-recognition/facenet-pytorch-vggface2-windows-native-cpu.json`

#### Windows Native CUDA Result

Manual smoke using the shell-installed runtime after upgrading torch to a
Blackwell-compatible CUDA build:

- Before final facenet smoke: `torch==2.11.0+cu130`,
  `torchvision==0.26.0+cu130`, `numpy==1.26.4`, `cv2==4.9.0`,
  `cuda_available=true`, GPU `NVIDIA GeForce RTX 5080`,
  `facenet-pytorch==2.6.0`.
- CUDA tensor allocation succeeded before the evaluator run.
- CUDA evaluator passed on all image and video fixtures: `mtcnn_device: "cuda"`,
  warm `254.5ms`, positive pair `0.9191`, negative pair `-0.0424`,
  `all_pairs_pass=true`.
- Raw output:
  `build/spikes/face-recognition/facenet-pytorch-vggface2-windows-native-cuda.json`

Windows NumPy/OpenCV finding:

- The installed runtime initially had `numpy==2.4.3` with an OpenCV wheel compiled against
  NumPy 1.x. Importing `cv2` emitted `_ARRAY_API not found` / NumPy ABI errors.
- Image face detection still completed and the required pair checks passed in
  that broken state, but video fixtures were skipped because `_load_pil()` could
  not use `cv2` to extract first frames.
- Pinning `numpy<2` resolved the issue: `numpy==1.26.4`, `cv2==4.9.0`, video
  frame extraction succeeded, and the full CUDA evaluator passed.
- This appears to be an artifact of the current Windows InsightFace path, not a
  facenet-pytorch requirement. `requirements-windows.txt` documents that
  `numpy==1.26.4` and `opencv-python-headless==4.9.0.80` are pinned for
  compatibility with the unofficial InsightFace 0.7.3 Windows wheel. If
  InsightFace is removed from the default runtime, revisit both pins together:
  either keep `numpy<2` while using OpenCV wheels compiled against NumPy 1.x, or
  upgrade OpenCV to a NumPy-2-compatible wheel before relaxing the NumPy pin.

#### Windows-specific targeted smoke

Recommended before implementation:

Use the **installed Windows bundle/runtime**, not a Windows-native dev checkout.
Windows native development is intentionally unsupported for this repo; the smoke
should validate the end-user installer layout under `%LOCALAPPDATA%`.

1. Install `facenet-pytorch` in the installed Windows runtime after the existing
   GPU-compatible `torch` / `torchvision` step, preferably with `--no-deps` or
   equivalent constraints.
2. Verify package versions from the installed venv: `torch` remains the
   Blackwell-compatible CUDA build selected by the installer, `torchvision`
   remains paired with that torch build, and CUDA still reports available on
   the known-good Windows CUDA machine.
3. Run one evaluator pass from the installed venv with `--device cuda` on the
   staged fixture set and confirm `mtcnn_device: "cuda"` plus pair scores
   passing. This is a smoke test, not a full re-spike.
4. Optionally run `--device cpu` once on Windows native to capture an installer
   timing estimate for non-CUDA users.

The existing installed-bundle validation harness is the right shape:
`tests/real_media/scripts/validate-installed-bundle-windows.ps1` installs a
bundle into isolated `AppDir` / `DataDir`, stages fixtures outside the app, and
runs commands through the installed venv. Extend that harness after the
implementation so it runs a tiny face-backend smoke from
`%LOCALAPPDATA%\MediaSearchAgent\.venv\Scripts\python.exe` or the isolated
`$appDir\.venv\Scripts\python.exe` used by the script.

For the current manually installed Windows shell bundle, use:

```powershell
powershell -ExecutionPolicy Bypass -File tests\real_media\scripts\smoke-installed-windows-facenet.ps1
```

The script uses the installed app runtime under `%LOCALAPPDATA%\MediaSearchAgent`
by default, verifies the shell installer's power-user CLI (`bin\msa.cmd`),
installs `facenet-pytorch` with `--no-deps`, verifies that `torch` /
`torchvision` did not change, then runs the spike evaluator inside a temporary
`cmd.exe` session that first `call`s `msa.cmd`. That lets the evaluator inherit
the same `MSA_*` environment variables that power users get from the installed
launcher while still using the cloned repo's real-media fixtures.

#### Future Windows Native CLI Runbook

Use this when re-running the spike from a Windows-native installed runtime. This
does not require or imply a Windows dev environment; it uses the shell
installer's power-user CLI and installed venv.

Open Command Prompt or PowerShell and verify the installed launcher:

```powershell
cd $env:LOCALAPPDATA\MediaSearchAgent
msa --help
```

For CUDA-capable RTX 5080 / Blackwell machines, first ensure the installed venv
has a torch build that supports `sm_120`:

```powershell
$uv = "$env:LOCALAPPDATA\MediaSearchAgent\uv\uv.exe"
$py = "$env:LOCALAPPDATA\MediaSearchAgent\.venv\Scripts\python.exe"

& $uv pip install --python $py --upgrade --force-reinstall torch torchvision `
  --index-url https://download.pytorch.org/whl/cu130

& $py -c "import torch; print(torch.__version__); print(torch.version.cuda); print(torch.cuda.is_available()); print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'NO CUDA'); print(torch.zeros(1, device='cuda'))"
```

The exact CUDA wheel line may change over time; the requirement is a PyTorch
wheel that supports Blackwell `sm_120`, not `cu130` specifically.

If OpenCV is installed from a NumPy-1-built wheel, keep NumPy below 2 before
running video fixtures:

```powershell
& $uv pip install --python $py "numpy<2"
& $py -c "import numpy, cv2; print(numpy.__version__); print(cv2.__version__)"
```

Run the installed-runtime smoke from a clone of this repo:

```powershell
powershell -ExecutionPolicy Bypass -File "\\wsl.localhost\Ubuntu\home\<user>\projects\media-search-agent\tests\real_media\scripts\smoke-installed-windows-facenet.ps1" -SkipInstall
```

Use `-RunCpu` to collect CPU timing too, or `-AllowNoCuda` on CPU-only machines:

```powershell
powershell -ExecutionPolicy Bypass -File "\\wsl.localhost\Ubuntu\home\<user>\projects\media-search-agent\tests\real_media\scripts\smoke-installed-windows-facenet.ps1" -RunCpu
powershell -ExecutionPolicy Bypass -File "\\wsl.localhost\Ubuntu\home\<user>\projects\media-search-agent\tests\real_media\scripts\smoke-installed-windows-facenet.ps1" -AllowNoCuda
```

Expected CUDA success signal:

```text
device=cuda mtcnn_device=cuda ... pairs=True
```

Also verify the before/after probe shows the same `torch` and `torchvision`
versions. If they changed, the facenet install path replaced the platform torch
stack and the smoke is not valid.

Windows-specific items to close during implementation:

**0. Shell installer torch variant**
Manual smoke on the current Windows shell-installed runtime showed
`torch==2.11.0+cpu`, `torchvision==0.26.0`, and `cuda_available=false` before
facenet install. Installing `facenet-pytorch --no-deps` preserved those versions
and installed cleanly, but the CUDA smoke could not run because the installed
runtime was CPU-only. This means the current shell installer is not exercising
the Blackwell-compatible CUDA torch path documented for the Windows native
installer. Either:

- update the shell installer to install `torch` / `torchvision` from the current
  Blackwell-compatible PyTorch CUDA index before facenet, or
- treat the shell installer as CPU-only for this smoke and validate CUDA through
  the Inno/native installer path.

**1. InsightFace unofficial wheel removal**
The current `install.ps1` downloads InsightFace from Gourieff's unofficial Windows
wheel host — a documented supply-chain concern (see `NOTICE` and compliance docs).
Replacing InsightFace with `facenet-pytorch` eliminates that step entirely.
Confirm `uv pip install facenet-pytorch` resolves cleanly from PyPI on Windows
native Python 3.12 without any unofficial index URL.
The Windows installed-runtime smoke confirmed the facenet path works after
installing `facenet-pytorch` from PyPI with `--no-deps`; no Gourieff wheel is
needed for the replacement backend.

**2. `opencv-python-headless` pin**
`requirements-windows.txt` pins `opencv-python-headless==4.9.0.80` with the
comment "required by InsightFace 0.7.3 unofficial wheel compatibility". If
InsightFace is removed, this pin can be relaxed. Verify the unpinned version
installs and that no other component depends on the 4.9.x version.

**3. `onnxruntime-gpu` removal**
`onnxruntime-gpu` is in `requirements-windows.txt` because InsightFace uses ONNX
Runtime for inference. If InsightFace moves to opt-in, confirm no other runtime
component (RT-DETR uses `transformers`/PyTorch, not onnxruntime; CLIP uses
`open_clip_torch`) requires it. If confirmed unused, removing `onnxruntime-gpu`
saves ~1 GB from the Windows install footprint.

**4. MTCNN on Windows CUDA**
On Windows with a CUDA GPU, MTCNN should run on GPU just as it did on WSL2.
Verify with a smoke run that reports `mtcnn_device: "cuda"`. Do not require a
new benchmark-quality performance result unless this differs materially from
WSL2.

**5. torch solver conflict on Windows**
On macOS, `uv pip install facenet-pytorch` downgraded torch from 2.11.0 → 2.2.2.
Confirm that installing `facenet-pytorch` cannot replace the platform-selected
torch stack. A version floor alone may be insufficient on Blackwell machines;
the installer must preserve the selected Blackwell-compatible torch index
selection.

#### WSL2/CUDA validation checks run

1. `MTCNN(device="cuda")` and `InceptionResnetV1(...).to("cuda")` both run
   without fallback or device mismatch errors.
2. Warm inference time is recorded with `mtcnn_device: "cuda"` in the JSON
   output.
3. Positive and negative pair scores remained above the same quality thresholds
   used on macOS.
4. The plain install path was tested and found unsafe because it downgrades
   `torch` / `torchvision`; implementation must install facenet without letting
   it replace the existing CUDA torch stack.

Recommended WSL2 command:

```bash
uv run python scripts/spike_face_recognizer_eval.py \
  --backend facenet_pytorch \
  --model vggface2 \
  --device cuda \
  --conf 0.8 \
  --fixtures tests/real_media/fixtures/originals \
  --output build/spikes/face-recognition/facenet-pytorch-vggface2-wsl2-cuda.json
```

**Recommended Windows approach:** run the same evaluator on the Windows CUDA
machine used for Windows-native validation. Repeat with `--device cpu` only if we want
a Windows-specific CPU timing estimate for setup messaging.

For a background indexer, ~1.5–2.5s/image on Intel i5 means 10K photos takes
4–7 hours — acceptable for an overnight first-index but should be surfaced in
the setup UI with an estimated time.

InsightFace uses `onnxruntime` for inference, which has an optimized ONNX Runtime
CPU path. facenet-pytorch uses native PyTorch — a larger CPU wheel (~250 MB
for torch+torchvision) but no additional build tools needed on Windows.

**Intel acceleration options (not in scope for v0.2.0):**

- Intel Extension for PyTorch (IPEX) with `torch.xpu` for Intel Arc discrete
  GPUs. Not yet stable enough to depend on.
- OpenVINO execution provider for ONNX Runtime (`onnxruntime-openvino`): valid
  for CPU and iGPU on Windows native. Requires converting MTCNN + ResNet to
  ONNX and moving inference outside WSL2. Architectural change — revisit if
  Intel laptop usage data from prerelease shows demand.
- DirectML (ONNX Runtime): deprecated; not a viable path.

**Recommended handling for v0.2.0:**

Surface estimated indexing time in the setup UI when CUDA is not available.
Do not disable face recognition — unlike object detection (where CLIP handles
most queries), the People page has no fallback once exposed.

## Stage 5: Recommendation

### Selected Default Backend: facenet-pytorch VGGFace2

`facenet-pytorch` (MTCNN + InceptionResnetV1 `vggface2`) is the recommended
permissive default backend. Key reasons:

- **Embedding quality matches or exceeds InsightFace**: positive pair similarity
  0.9203 vs 0.8821, negative pair −0.0464 vs −0.0176. Cluster separation in the
  People page is preserved.
- **512-dim embeddings** — same dimension as InsightFace, so no Qdrant schema
  dimension change is needed. Existing face vectors still require a rebuild
  because the embedding space changes.
- **Pure PyTorch** — no `onnxruntime` dependency; removes a platform-specific
  install split (macOS needs `onnxruntime` not `onnxruntime-gpu`).
- **Removes the unofficial Windows InsightFace wheel from the default path** —
  Windows can install the face backend from PyPI using the same PyTorch stack as
  the rest of the ML runtime, eliminating a supply-chain and maintenance concern.
- **MIT license** (code + package-distributed weights). Weight provenance must
  be confirmed at the model card URL before merge.
  URL: `https://github.com/timesler/facenet-pytorch`
- **3.97s → 0.37s startup** — model load is 10× faster than InsightFace.

### Known Issues for the Implementation Phase

1. **Confidence threshold**: raise default from 0.70 → **0.80** to eliminate
   the two false positives observed on dog fixtures (confs 0.70–0.74) without
   losing any genuine face detections (all measured at ≥ 0.81).

2. **MTCNN CPU-only on macOS MPS**: PyTorch MPS does not support adaptive
   pooling (pytorch#96056). MTCNN must always be created with `device="cpu"`.
   InceptionResnetV1 can still run on MPS for the embedding step. This is not a
   blocker — it means throughput on macOS without CUDA is ~10× slower than
   InsightFace. Surface estimated time in setup UI.

3. **torch solver conflict**: `uv pip install facenet-pytorch` resolves torch
   downward to 2.2.2 via the torchvision dependency chain. A generic
   `torch>=2.4.0` floor is not enough for Blackwell GPUs; installers must
   preserve the platform-selected CUDA torch build that supports `sm_120`
   (`torch==2.11.0+cu130` in the WSL2 run; future Windows builds may use a newer
   compatible CUDA wheel). Install facenet without dependency resolution
   replacing torch. facenet 2.6.0 runs correctly on torch 2.11.0 as confirmed
   in this spike.

4. **Age/gender metadata**: facenet-pytorch does not provide age or gender
   estimation. The `metadata` dict in `FaceDetection` will be empty. Verify
   whether any UI component currently renders these fields before removing them
   from the API schema.

### Optional Backends

- `insightface` / `buffalo_l`: retained as opt-in for users who accept
  non-commercial terms or hold a commercial license. Keep behind a config flag
  `face_recognizer_backend: insightface` with a visible non-commercial warning.
- `deepface` / `GhostFaceNet`: runner-up — passes all quality thresholds and
  has zero false positives — but the TensorFlow dependency (~500 MB install) is
  a blocker for a lightweight cross-platform installer. The positive pair
  similarity (0.7947) is also materially lower than facenet-pytorch (0.9203),
  which would produce noticeably weaker cluster separation on large libraries.
  Consider revisiting if facenet-pytorch has deployment blockers on a specific
  platform and a TF-free deepface version ships.
- `opencv_sface`: rejected as primary backend due to detection gaps
  (`face_expressive_01.jpg` and `face_single_02.jpg` both missed). May be
  revisited for constrained install-size scenarios where the 39 MB footprint
  and zero-PyTorch dependency matter more than peak detection quality.

### Dependency Changes

Add to default requirements:

- `facenet-pytorch>=2.6.0`

Move to optional/extras:

- `insightface` (non-commercial; only if user opts in)
- `onnxruntime` / `onnxruntime-gpu` (no longer needed if InsightFace is opt-in only)

### Installer Changes

- Remove automatic download and SHA-256 verification of InsightFace buffalo_l
  ONNX files from `setup_models.py`.
- Replace with facenet-pytorch model download: `InceptionResnetV1(pretrained="vggface2")`
  downloads ~107 MB to `~/.cache/torch/hub/checkpoints/`. The implementation
  phase should redirect this to `models_dir` — torch hub supports a custom
  directory via `torch.hub.set_dir()` or the `TORCH_HOME` environment variable.
  MTCNN weights (~1 MB) download automatically from the package's own CDN on
  first use.
- Document model sizes in setup UI: facenet-pytorch total ~108 MB
  vs InsightFace buffalo_l ~341 MB (extracted).

### Notice / License Changes

- Remove InsightFace non-commercial notice from default runtime path (retain in
  the opt-in documentation).
- Add attribution for `facenet-pytorch` (MIT, timesler) and the VGGFace2
  pretrained weights in `NOTICE`.
- Confirm weight file license at `https://github.com/timesler/facenet-pytorch`
  before merging the implementation.

### Migration Plan for Existing `face_model: buffalo_l` Config Values

Config values `buffalo_l`, `buffalo_s`, `antelopev2` will be invalid once
InsightFace is no longer the default backend. On startup, emit a migration
warning explaining the backend change, then fall back to the new default
(`face_recognizer_backend: facenet_pytorch`, `face_model: vggface2`).

Existing indexed face embeddings (512-dim InsightFace ArcFace space) are **not
compatible** with facenet-pytorch embeddings (512-dim FaceNet space). The Qdrant
`face_emb` collection must be dropped and rebuilt. Show a one-time notice in the
UI explaining the re-index requirement and why.

## Open Questions

- If the replacement drops age/gender estimation: should the metadata fields be
  removed from the API response schema, or kept as nullable? Check whether any
  UI component renders gender/age before deciding.
- Should InsightFace remain available as an explicit opt-in backend for users
  who accept non-commercial terms? A `face_recognizer_backend: insightface`
  config key would let power users keep the current quality.
- If `onnxruntime` is removed as a direct dependency (because facenet-pytorch
  uses native PyTorch), does any other component still require it?
  Check `requirements-api.txt` and `requirements-indexer.txt`.
- Should the Qdrant face collection be automatically rebuilt when the backend
  changes (different embedding space), or should it warn and require a manual
  reindex?

## Acceptance Criteria

The spike is complete when:

- At least one permissive candidate has measured results.
- That candidate meets the embedding quality minimum pass (positive pairs
  ≥ 0.60, negative pairs < 0.55) on the baseline fixture set.
- WSL2/CUDA validation confirms facenet-pytorch uses CUDA for both MTCNN
  detection and InceptionResnetV1 embedding, with pair scores still passing.
- Windows native targeted smoke confirms facenet-pytorch can be installed
  without replacing the Blackwell-compatible CUDA torch stack, CUDA still works,
  and one CUDA evaluator pass reports `mtcnn_device: "cuda"` with pair scores
  passing.
- License posture is documented for both code and pretrained weights with
  source URLs.
- Runtime and installer implications are documented for Linux/WSL2, Windows
  native Intel, and macOS.
- A follow-up implementation issue can be estimated.

## Follow-Up Implementation Plan

If the spike selects a replacement:

1. Introduce `FaceBackend` protocol and backend registry in
   `src/msa_indexer/models/faces.py`.
2. Add `face_recognizer_backend` config key; keep `face_model` as a per-backend
   sub-config (e.g. `vggface2` for facenet-pytorch).
3. Update `setup_models.py`: replace InsightFace ONNX SHA-256 verification with
   the replacement model's download and integrity check.
4. Remove `insightface` and `onnxruntime` from default requirements (or move
   to optional extras) if no longer needed.
5. If embedding dimension changes: add migration path that drops and rebuilds
   the Qdrant face collection with a user-visible warning.
6. Update `NOTICE`, compliance docs, `docs/ML_MODEL_CACHE.md`, and installer
   docs.
7. Update face recognition tests.
8. If InsightFace is kept as opt-in: document the non-commercial restriction
   visibly in the UI and `config.yaml` comments.
