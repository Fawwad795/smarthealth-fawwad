"""Download the embedding model at image build time.

LocalEmbeddings runs all-MiniLM-L6-v2 inside the app's own process, and
fastembed would otherwise fetch the model (87MB) from the Hugging Face Hub
the first time a container asked for it. Fetching it here means a running
container never needs the network for embeddings: the Dockerfile sets
HF_HUB_OFFLINE=1 after this step, so a model missing from the image fails
loudly instead of being downloaded mid-publish.

Downloading a model file from the Hub needs no key and uses no inference
credits; only Hugging Face's hosted inference does.
"""

import os

from fastembed import TextEmbedding

# Must match Settings.embedding_model's default; a unit test checks it.
# A different EMBEDDING_MODEL needs this changed and the image rebuilt,
# just as it needs a migration for the vector column's size.
MODEL = "sentence-transformers/all-MiniLM-L6-v2"


def main() -> None:
    """Load the model once, which downloads it into EMBEDDING_MODEL_DIR."""
    cache_dir = os.environ["EMBEDDING_MODEL_DIR"]
    model = TextEmbedding(MODEL, cache_dir=cache_dir)
    # One embedding proves the files that landed actually load and run.
    dimensions = len(next(iter(model.embed(["build check"]))))
    print(f"{MODEL} ready in {cache_dir} ({dimensions} dimensions)")


if __name__ == "__main__":
    main()
