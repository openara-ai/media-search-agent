# src/msa_indexer/notebook_utils.py
from __future__ import annotations
from pathlib import Path
import sqlite3
import numpy as np
import faiss
import torch, open_clip
from PIL import Image
from pillow_heif import register_heif_opener

register_heif_opener()  # enable HEIC/HEIF in Pillow

# ---------- IDs & mappings ----------

def mediaid_to_faissid(media_id: str) -> np.int64:
    # must match the hash you used during indexing
    return np.int64(int.from_bytes(media_id.encode()[:8].ljust(8, b"\0"), "big"))

def build_faissid_to_path(sqlite_path: Path) -> dict[np.int64, str]:
    con = sqlite3.connect(str(sqlite_path))
    rows = con.execute("SELECT media_id, path FROM media").fetchall()
    con.close()
    return { mediaid_to_faissid(mid): p for (mid, p) in rows }

# ---------- CLIP model ----------

def load_clip(model_name: str = "ViT-L-14",
              pretrained: str = "laion2b_s32b_b82k",
              device: str | None = None):
    device = device or (
        "cuda" if torch.cuda.is_available()
        else "mps" if torch.backends.mps.is_available()
        else "cpu"
    )
    model, _, preprocess = open_clip.create_model_and_transforms(
        model_name, pretrained=pretrained, device=device
    )
    tokenizer = open_clip.get_tokenizer(model_name)
    return model, preprocess, tokenizer, device

# ---------- Embedding helpers ----------

def embed_text(model, tokenizer, device, texts: list[str]) -> np.ndarray:
    with torch.no_grad():
        tok = tokenizer(texts).to(device)
        vec = model.encode_text(tok).float().cpu().numpy()
    faiss.normalize_L2(vec)
    return vec

def embed_images(model, preprocess, device, paths: list[str]) -> np.ndarray:
    ims = []
    for p in paths:
        try:
            img = Image.open(p).convert("RGB")
            ims.append(preprocess(img))
        except Exception:
            pass
    if not ims:
        return np.zeros((0, model.text_projection.shape[1]), dtype="float32")
    batch = torch.stack(ims).to(device)
    with torch.no_grad():
        vec = model.encode_image(batch).float().cpu().numpy()
    faiss.normalize_L2(vec)
    return vec

# ---------- FAISS ----------

def load_faiss_index(path: Path):
    index = faiss.read_index(str(path))
    return index

def search(index, query_vec: np.ndarray, k: int = 8):
    D, I = index.search(query_vec.astype("float32"), k)
    return D, I

# ---------- Display ----------

def show_paths(paths: list[str], max_side: int = 512):
    from IPython.display import display
    for p in paths:
        try:
            im = Image.open(p).convert("RGB")
            im.thumbnail((max_side, max_side))
            display(im)
        except Exception as e:
            print("display fail:", p, e)

# ---------- PCA plot ----------

def plot_pca(image_vecs: np.ndarray, text_vecs: np.ndarray, queries: list[str]):
    from sklearn.decomposition import PCA
    import matplotlib.pyplot as plt

    X = np.vstack([image_vecs, text_vecs])
    pca = PCA(n_components=2, random_state=0)
    X2 = pca.fit_transform(X)
    img2, txt2 = X2[:len(image_vecs)], X2[len(image_vecs):]

    plt.figure(figsize=(9, 7))
    plt.scatter(img2[:,0], img2[:,1], s=12, alpha=0.55, label="images")
    plt.scatter(txt2[:,0], txt2[:,1], s=80, marker="X", label="queries")
    for (x, y), q in zip(txt2, queries):
        plt.text(x, y, "  " + q, fontsize=9)
    plt.title("CLIP embedding space: images vs. text queries (PCA 2D)")
    plt.xlabel("PC1"); plt.ylabel("PC2"); plt.legend()
    plt.tight_layout()
    plt.show()
