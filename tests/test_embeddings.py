import torch
import numpy as np

from msa_indexer.models.embeddings import ClipEmbedder


def test_text_embed_single_and_batch():
    """Unit test for ClipEmbedder.text_embed using a DummyModel/tokenizer.

    This test avoids network downloads by constructing a ClipEmbedder instance
    without running __init__ and injecting a small dummy model and tokenizer.
    """
    # Create a bare-bones instance without invoking open_clip model loading
    emb = ClipEmbedder.__new__(ClipEmbedder)

    # Dummy model: must expose text_projection.shape and encode_text()
    class DummyModel:
        def __init__(self, dim=128):
            # text_projection must have shape (_, dim)
            self.text_projection = torch.zeros((1, dim))

        def encode_text(self, tokens):
            # tokens is a tensor of shape (N, L)
            n = tokens.shape[0] if isinstance(tokens, torch.Tensor) else len(tokens)
            return torch.ones((n, self.text_projection.shape[1]), dtype=torch.float32)

    # Inject dummy model and tokenizer
    emb.model = DummyModel(dim=128)
    emb.tokenizer = lambda texts: torch.randint(0, 100, (len(texts), 77))
    emb.device = "cpu"

    # Single text
    out = emb.text_embed("a photo of a beach")
    assert isinstance(out, np.ndarray)
    assert out.shape == (1, emb.model.text_projection.shape[1])

    # Multiple texts
    out2 = emb.text_embed(["cat", "dog", "bird"])
    assert out2.shape == (3, emb.model.text_projection.shape[1])
