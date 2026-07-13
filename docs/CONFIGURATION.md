# Configuration

Reference for the `config.yaml` file that controls media sources, model
selection, ports, and search behaviour.

Most users only ever touch the **Indexer** page in the UI to add media
folders — that writes the relevant entries into `config.yaml` for you.
Everything else has sensible defaults. This page covers the file directly
for the cases when you want to tune something.

## Where it lives

| Platform | Path |
|---|---|
| **macOS** | `~/Library/Application Support/MediaSearchAgent/config.yaml` |
| **Windows** | `%USERPROFILE%\MediaSearchAgent\config.yaml` |
| **Linux** | `~/.config/MediaSearchAgent/config.yaml` |

The file is created on first run from the platform-specific template shipped
with the app. It is never overwritten by updates or reinstalls.

## When changes take effect

- **Desktop app (macOS / Windows)** — quit the app and relaunch it.
- **Browser / headless installs** — restart the server (`msa api restart`).
- **Indexer** — picks up the latest config every time you click **Run**.

The UI does not hot-reload `config.yaml`.

## Media sources

Folders the indexer walks. You can have any number of them and they all
flow into the same searchable library.

```yaml
media_sources:
  - name: "Photos"
    path: "/Users/me/Pictures"
    read_only: true
  - name: "External Drive"
    path: "/Volumes/Backup/Photos"
    read_only: true
```

| Field | Purpose |
|---|---|
| `name` | Display label in the UI. Any string. |
| `path` | Absolute path to the folder. Use the format native to your OS — Windows users use `D:\Photos`, macOS/Linux use `/Users/...` or `/mnt/...`. |
| `read_only` | When `true` (the default and recommended), the indexer will refuse to write to anything under this path. Leave it on unless you have a specific reason. |

The Indexer page in the UI manages this list for you with a directory
picker — you rarely need to edit it by hand.

## CLIP model

The vision-language model that powers semantic search.

```yaml
model_name: "ViT-L-14"
pretrained: "openai"
```

| Setting | Default | Notes |
|---|---|---|
| `model_name` | `ViT-L-14` | Best search quality. `ViT-B-32` is a smaller, faster fallback for low-RAM machines. |
| `pretrained` | `openai` | Weight set. Other options exist via `open_clip_torch` but most people stay on the default. |

Changing the model invalidates existing embeddings — the indexer will
re-embed on the next run.

## Compute

```yaml
device: "auto"
batch_size: 32
num_workers: 4
```

| Setting | Default | Notes |
|---|---|---|
| `device` | `auto` | Picks CUDA if available, then MPS (Apple Silicon), else CPU. Set to `cuda`, `mps`, or `cpu` to force. |
| `batch_size` | `32` | How many images go through the model at once during indexing. Lower if you hit OOM on a small GPU. |
| `num_workers` | `4` | Data-loader worker processes. Drop to `2` on machines with few cores. |

## Object detection

```yaml
enable_object_detection: auto
object_detector_backend: "rtdetr"
object_model: "PekingU/rtdetr_r18vd"
object_confidence_threshold: 0.35
enable_video_object_detection: true
video_detection_max_frames: 10
```

| Setting | Default | Notes |
|---|---|---|
| `enable_object_detection` | `auto` | `auto` enables it when a GPU is detected and skips it on CPU-only machines (RT-DETR is too slow without one). `true` forces it on; `false` disables. |
| `object_model` | `PekingU/rtdetr_r18vd` (81 MB) | Trade size for quality: `rtdetr_r34vd` (126 MB) or `rtdetr_r50vd` (172 MB). |
| `object_confidence_threshold` | `0.35` | Tags below this score are dropped. Raise to reduce false positives, lower to surface more tags. |
| `video_detection_max_frames` | `10` | Cap on keyframes-per-video that get tagged, to keep video indexing tractable. |

## Face recognition

```yaml
enable_face_recognition: true
face_recognizer_backend: "facenet_pytorch"
face_model: "vggface2"
face_confidence_threshold: 0.80
face_min_size: 20
```

| Setting | Default | Notes |
|---|---|---|
| `face_recognizer_backend` | `facenet_pytorch` | MIT-licensed, MTCNN detector + InceptionResnetV1 / VGGFace2 embeddings. The recommended default. |
| `face_confidence_threshold` | `0.80` | Below this, detections are dropped to avoid false-positive faces on non-face objects. |
| `face_min_size` | `20` | Minimum face size in pixels. Smaller faces are skipped. |

## Video shot detection

Videos are split into shots (PySceneDetect), then keyframes are sampled per
shot and embedded.

```yaml
enable_video_shot_detection: true
shot_detection_threshold: 30.0
min_shot_length_frames: 15
keyframes_per_shot: 1
```

The defaults work well across most consumer video. Lower
`shot_detection_threshold` for content with subtle cuts; raise it if you're
seeing too many fragmentary shots.

## Server / API

These settings apply to browser and headless installs (`msa api start`).
The **desktop app ignores them** — it binds a private local port of its own
on every launch, so it never conflicts with anything else on your machine.

```yaml
api:
  host: "127.0.0.1"
  port: 8000

ui:
  api_url: "http://localhost:8000"
```

| Setting | Default | Notes |
|---|---|---|
| `api.host` | `127.0.0.1` | Bind address. Set to `0.0.0.0` to accept connections from other devices on your LAN. There is no auth in front of the API — only do this on a trusted network. |
| `api.port` | `8000` | Change if another app already uses 8000. Update `ui.api_url` to match. |
| `ui.api_url` | `http://localhost:8000` | The URL the SPA fetches from. Must match `api.host` / `api.port`. |

## Retrieval tuning

```yaml
retrieval:
  top_k_candidates: 200
  top_k_return: 50
```

| Setting | Default | Notes |
|---|---|---|
| `top_k_candidates` | `200` | How many vectors Qdrant returns before reranking. Higher = better recall, slower queries. |
| `top_k_return` | `50` | How many results the API hands back to the UI. The UI's infinite scroll fetches more on demand. |

## Logging

```yaml
log_level: "INFO"
log_dir: "logs"
```

`log_level` accepts `DEBUG`, `INFO`, `WARNING`, `ERROR`. `log_dir` is
relative to the data directory.

## Storage paths

The installer sets these to platform-appropriate locations and you should
not normally need to touch them:

```yaml
index_dir: "index"
sqlite_path: "index/media.sqlite"
qdrant_path: "qdrant"
thumb_dir: "data/thumbnails"
face_thumb_dir: "data/face_thumbnails"
models_dir: "models"
```

Override only if you want to point the indexer at an external drive — e.g.
`qdrant_path: "/Volumes/Fast/MediaSearchAgent/qdrant"`.

## Full reference

The shipped `config.yaml` has inline comments for every setting, including
some advanced knobs (face clustering, ANN tuning, Qdrant collection names)
not covered here. Open it directly to see the complete list with defaults.
