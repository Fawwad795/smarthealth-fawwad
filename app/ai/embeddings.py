"""Turns text into vectors, behind one small interface.

Everything that needs an embedding (the publish workflow in task 4.5,
search in 4.8, the eval script in 4.10) depends on EmbeddingProvider and
never on a vendor. get_embedding_provider() picks the implementation from
settings: LocalEmbeddings by default, which runs the model in this process,
or HuggingFaceEmbeddings, which calls Hugging Face's hosted inference. Tests
construct FakeEmbeddings directly, so the suite needs no network and no key.

Error messages carry status codes and counts, never the input texts: a
search query is the patient's own words (rule 6.6).
"""

import functools
import hashlib
import math
import os
import re
from abc import ABC, abstractmethod

import httpx
from fastembed import TextEmbedding

from app.core.config import settings


class EmbeddingError(Exception):
    """Base class for every embedding failure, so a caller can catch one type."""


class TransientEmbeddingError(EmbeddingError):
    """A failure that may succeed on retry: a timeout, a dropped connection or
    failed TLS handshake, rate limiting (429), or a server error (5xx,
    including the 503 Hugging Face returns while a model loads).
    """


class PermanentEmbeddingError(EmbeddingError):
    """A failure no retry can fix: a missing or rejected key, an unknown
    model, or a response that does not match the configured vector size.
    Task 4.5 marks these non-retryable, so a bad key fails a publish once
    instead of retrying until the workflow gives up.
    """


class EmbeddingProvider(ABC):
    """The contract every embedding implementation keeps.

    An ABC rather than a typing.Protocol: a subclass that forgets embed()
    fails the moment it is constructed. A Protocol is only checked by mypy,
    which this repo runs on app/models alone.
    """

    def __init__(self, model: str, dimensions: int) -> None:
        """Record the model name and vector size. Both are stored beside
        every vector, so a later model change can find what is stale.
        """
        self.model = model
        self.dimensions = dimensions

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one vector of `self.dimensions` floats per text, in the
        same order as `texts`.
        """


class HuggingFaceEmbeddings(EmbeddingProvider):
    """Embeds through Hugging Face's hosted feature-extraction endpoint.

    Sends up to `batch_size` texts per HTTP request. One request per text
    would pay the round trip and the rate limit once per service, which the
    brief lists as a common mistake. For a sentence-transformers model the
    endpoint returns one pooled vector per input, already scaled to length 1.
    """

    BASE_URL = "https://router.huggingface.co/hf-inference/models"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        dimensions: int,
        batch_size: int,
        timeout_seconds: float,
        client: httpx.Client | None = None,
    ) -> None:
        """Fail at construction if the key is missing or unusable. The
        publish Activity builds the provider on each attempt, so a bad key
        fails that publish once, with this message, instead of every request
        failing and being retried.

        An HTTP header can only carry ASCII. A key holding anything else
        (seen when a container kept an old .env line whose comment had an
        em dash) would otherwise fail inside httpx on every attempt.

        `client` exists for the tests, which pass one backed by
        httpx.MockTransport so no request leaves the process.
        """
        super().__init__(model, dimensions)
        if not api_key:
            raise PermanentEmbeddingError("EMBEDDING_API_KEY is empty; set it in .env")
        if not api_key.isascii():
            raise PermanentEmbeddingError(
                "EMBEDDING_API_KEY contains a non-ASCII character, which an "
                "HTTP header cannot carry; check its value in .env"
            )
        self._url = f"{self.BASE_URL}/{model}/pipeline/feature-extraction"
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._batch_size = batch_size
        self._client = client or httpx.Client(timeout=timeout_seconds)

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed `texts` in batches of `batch_size`, one request per batch."""
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            vectors.extend(self._embed_batch(texts[start : start + self._batch_size]))
        return vectors

    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        """Send one request and sort any failure into transient or permanent."""
        try:
            response = self._client.post(
                self._url, headers=self._headers, json={"inputs": batch}
            )
        except httpx.TransportError as exc:
            # Timeouts, refused connections and failed TLS handshakes all
            # land here; a handshake timeout was seen on Week 4 Day 1.
            raise TransientEmbeddingError(
                f"Hugging Face unreachable ({type(exc).__name__})"
            ) from exc

        # The body is deliberately left out of both messages: an error
        # response may echo the request, and a query is patient text.
        status_code = response.status_code
        if status_code == 429 or status_code >= 500:
            raise TransientEmbeddingError(
                f"Hugging Face returned HTTP {status_code} for {len(batch)} texts"
            )
        if status_code != 200:
            raise PermanentEmbeddingError(
                f"Hugging Face returned HTTP {status_code} for {len(batch)} texts"
            )
        return self._checked(response, expected=len(batch))

    def _checked(self, response: httpx.Response, expected: int) -> list[list[float]]:
        """Return the vectors, or raise if they are not `expected` lists of
        `self.dimensions` numbers.

        A wrong shape means the model or the configured size is wrong, which
        no retry fixes; catching it here gives a clear message instead of a
        pgvector dimension error later.
        """
        try:
            vectors = response.json()
        except ValueError as exc:
            raise PermanentEmbeddingError("Hugging Face returned non-JSON") from exc
        if not isinstance(vectors, list) or len(vectors) != expected:
            raise PermanentEmbeddingError(
                f"expected {expected} vectors from {self.model}"
            )
        for vector in vectors:
            if not isinstance(vector, list) or len(vector) != self.dimensions:
                raise PermanentEmbeddingError(
                    f"expected {self.dimensions}-number vectors from {self.model}"
                )
        return vectors


@functools.lru_cache(maxsize=None)
def _load_local_model(model: str) -> TextEmbedding:
    """Load `model` from the image, once per process.

    Loading reads 87MB from disk and takes about 0.6 s, while embedding one
    service takes milliseconds, and the publish Activity builds a provider
    on every attempt. Cached per process, so the API and the worker each
    load it once. A failed load is not cached, so the next attempt tries
    again.

    The Dockerfile downloads the model into EMBEDDING_MODEL_DIR at build
    time and then sets HF_HUB_OFFLINE, so any failure here means the model
    is not in the image. No retry can fix that, hence permanent.
    """
    try:
        return TextEmbedding(model, cache_dir=os.getenv("EMBEDDING_MODEL_DIR") or None)
    except Exception as exc:
        raise PermanentEmbeddingError(
            f"could not load {model} ({type(exc).__name__}); the image downloads "
            "it at build time, so rebuild it after changing EMBEDDING_MODEL"
        ) from None


class LocalEmbeddings(EmbeddingProvider):
    """Runs the model inside this process with fastembed (ONNX Runtime).

    The default provider. It needs no key, has no quota and makes no network
    call, so a demo cannot be stopped by an outside service. The same
    all-MiniLM-L6-v2 model gave the same scores, to three decimals, as Hugging
    Face's hosted inference did on Week 4 Day 1 (docs/ai-layer.md section 3).
    """

    def __init__(
        self,
        *,
        model: str,
        dimensions: int,
        batch_size: int,
        encoder: TextEmbedding | None = None,
    ) -> None:
        """Load the model, or take `encoder`, which the tests pass instead."""
        super().__init__(model, dimensions)
        self._batch_size = batch_size
        self._encoder = encoder or _load_local_model(model)

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed `texts` in batches of `batch_size`, in this process.

        fastembed returns numpy arrays; converted to plain lists of floats so
        callers get the same type from every provider.
        """
        vectors = [
            vector.tolist()
            for vector in self._encoder.embed(texts, batch_size=self._batch_size)
        ]
        if len(vectors) != len(texts) or any(
            len(vector) != self.dimensions for vector in vectors
        ):
            raise PermanentEmbeddingError(
                f"expected {len(texts)} vectors of {self.dimensions} numbers "
                f"from {self.model}"
            )
        return vectors


# Lower-cased runs of letters and digits: "X-Ray" gives "x" and "ray".
_WORD = re.compile(r"[a-z0-9]+")


class FakeEmbeddings(EmbeddingProvider):
    """A network-free stand-in for tests, built with the hashing trick.

    Each word is hashed to one of `dimensions` positions and counted there,
    then the vector is scaled to length 1, like the real model's output.
    Texts that share words point partly the same way, so a search test can
    check that "knee scan" ranks Knee X-Ray first. It matches spelling and
    knows nothing about meaning.

    Uses hashlib rather than the built-in hash(): hash() of a string is
    salted per process (PYTHONHASHSEED), so its vectors would change from
    one test run to the next.
    """

    def __init__(self, dimensions: int = 384) -> None:
        """Start with an empty call log; the model name marks fake vectors
        so they can never pass for real ones in the database.
        """
        super().__init__(model="fake-hashing-v1", dimensions=dimensions)
        # Every batch embed() received, so a test can check what was sent,
        # or that nothing was (task 4.9 skips unchanged chunks).
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Record the batch, then embed each text independently."""
        self.calls.append(list(texts))
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        """Count each word at its hashed position, then scale to length 1."""
        vector = [0.0] * self.dimensions
        # A text with no words still needs a direction: pgvector returns NaN
        # for the cosine distance to an all-zero vector.
        tokens = _WORD.findall(text.lower()) or [text]
        for token in tokens:
            digest = hashlib.sha256(token.encode()).digest()
            vector[int.from_bytes(digest[:8], "big") % self.dimensions] += 1.0
        length = math.sqrt(sum(x * x for x in vector))
        return [x / length for x in vector]


def get_embedding_provider() -> EmbeddingProvider:
    """Build the provider EMBEDDING_PROVIDER names.

    The one place that reads the embedding settings, so swapping providers
    is a config change. Settings validation has already rejected any other
    value, so these three branches are complete.
    """
    if settings.embedding_provider == "fake":
        return FakeEmbeddings(settings.embedding_dimensions)
    if settings.embedding_provider == "local":
        return LocalEmbeddings(
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
            batch_size=settings.embedding_batch_size,
        )
    return HuggingFaceEmbeddings(
        api_key=settings.embedding_api_key.get_secret_value(),
        model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
        batch_size=settings.embedding_batch_size,
        timeout_seconds=settings.embedding_timeout_seconds,
    )
