"""Pinned local Qwen document embedding; optional SDKs load only on initialize."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar, cast

from atlasrag.domain.indexing.failures import (
    EmbeddingInputLimitError,
    EmbeddingOutputError,
)
from atlasrag.domain.indexing.models import EmbeddingModelIdentity
from atlasrag.providers.embedding.base import validate_embedding_output
from atlasrag.providers.embedding.profile import EmbeddingExecutionProfile

QWEN_MODEL_ID = "Qwen/Qwen3-Embedding-0.6B"
QWEN_REVISION = "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"
QWEN_DIMENSION = 1024
_ASSETS = [
    "config.json",
    "model.safetensors",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.json",
    "merges.txt",
    "special_tokens_map.json",
    "added_tokens.json",
]
_T = TypeVar("_T")


@contextmanager
def _settle_cuda(torch: Any, device: str) -> Iterator[None]:
    """Wait for issued kernels even if forward/pooling/initialization raises."""
    try:
        yield
    finally:
        if device.startswith("cuda"):
            # A synchronization failure is technical and must propagate, even
            # when an earlier shape check found a controlled output failure.
            torch.cuda.synchronize(device)


async def _settled_thread(operation: Callable[[], _T]) -> _T:
    """Cancellation never releases a resource while its worker is still using it."""

    def run() -> tuple[_T | None, BaseException | None]:
        try:
            return operation(), None
        except BaseException as error:
            # concurrent.futures otherwise replaces TimeoutError instances.
            # Carry the original failure across the thread boundary unchanged.
            return None, error

    task = asyncio.create_task(asyncio.to_thread(run))
    interrupted: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            if interrupted is None:
                interrupted = error
    result, failure = task.result()
    if interrupted is not None:
        if failure is not None:
            interrupted.add_note(
                f"settled embedding worker failed: {type(failure).__name__}"
            )
        raise interrupted
    if failure is not None:
        raise failure
    return cast(_T, result)


@dataclass(frozen=True, slots=True)
class _Loaded:
    torch: Any
    tokenizer: Any
    model: Any
    identity: EmbeddingModelIdentity


class QwenEmbeddingProvider:
    """One explicitly loaded model, one inference slot, one bounded batch per call."""

    def __init__(
        self, *, device: str, profile: EmbeddingExecutionProfile | None = None
    ) -> None:
        if (
            not isinstance(device, str)
            or re.fullmatch(r"cpu|cuda(?::[0-9]+)?", device) is None
        ):
            raise ValueError("embedding device must be explicit cpu or cuda[:index]")
        self._device = device
        self._profile = EmbeddingExecutionProfile.model_validate(
            profile if profile is not None else EmbeddingExecutionProfile()
        )
        self._semaphore = asyncio.Semaphore(1)
        self._loaded: _Loaded | None = None

    @property
    def identity(self) -> EmbeddingModelIdentity:
        if self._loaded is None:
            raise RuntimeError("embedding provider must be initialized explicitly")
        return self._loaded.identity

    async def initialize(self, *, allow_download: bool = False) -> None:
        """Load pinned assets once; downloads require explicit caller authorization."""
        if type(allow_download) is not bool:
            raise TypeError("allow_download must be a boolean")
        async with self._semaphore:
            if self._loaded is None:
                await _settled_thread(lambda: self._initialize_sync(allow_download))

    def _initialize_sync(self, allow_download: bool) -> None:
        import torch  # type: ignore[import-not-found]
        from huggingface_hub import snapshot_download  # type: ignore[import-not-found]
        from transformers import (  # type: ignore[import-not-found]
            AutoModel,
            AutoTokenizer,
        )

        cuda = self._device.startswith("cuda")
        if cuda and not torch.cuda.is_available():
            raise RuntimeError("requested CUDA embedding device is unavailable")
        requested = self._device
        if requested == "cuda":
            requested = f"cuda:{torch.cuda.current_device()}"
        device = torch.device(requested)
        dtype = torch.float16 if cuda else torch.float32
        snapshot = snapshot_download(
            repo_id=QWEN_MODEL_ID,
            revision=QWEN_REVISION,
            local_files_only=not allow_download,
            allow_patterns=_ASSETS,
        )
        # Public snapshot_download returns the resolved commit directory. Both
        # loaders consume that exact snapshot, never a mutable ref or config's
        # obsolete private _commit_hash field (removed in Transformers 5).
        path = Path(snapshot)
        if path.name != QWEN_REVISION or path.parent.name != "snapshots":
            raise RuntimeError(
                "embedding snapshot did not resolve to the pinned revision"
            )
        tokenizer = AutoTokenizer.from_pretrained(
            snapshot,
            local_files_only=True,
            trust_remote_code=False,
            padding_side="left",
        )
        with _settle_cuda(torch, str(device)):
            model = (
                AutoModel.from_pretrained(
                    snapshot,
                    local_files_only=True,
                    trust_remote_code=False,
                    dtype=dtype,
                    attn_implementation="sdpa",
                )
                .to(device)
                .eval()
            )
            if (
                model.config.hidden_size != QWEN_DIMENSION
                or model.config.max_position_embeddings != 32768
            ):
                raise RuntimeError(
                    "loaded embedding model dimension or context differs from baseline"
                )
            parameters = tuple(model.parameters())
            if not parameters or any(
                str(parameter.device) != str(device) or parameter.dtype != dtype
                for parameter in parameters
            ):
                raise RuntimeError(
                    "loaded embedding model device or dtype differs from requested execution"
                )
            identity = EmbeddingModelIdentity(
                model_id=QWEN_MODEL_ID,
                tokenizer_id=QWEN_MODEL_ID,
                model_revision=path.name,
                tokenizer_revision=path.name,
                dimension=model.config.hidden_size,
                device=str(parameters[0].device),
            )
        self._loaded = _Loaded(torch, tokenizer, model, identity)

    async def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        """Embed unchanged document text; no query instructions or truncation."""
        if isinstance(texts, (str, bytes)) or not isinstance(texts, Sequence):
            raise TypeError("embedding input must be an ordered sequence of text")
        if not 1 <= len(texts) <= self._profile.batch_size:
            raise ValueError("embedding input must fit the configured nonempty batch")
        batch = tuple(texts)
        if any(not isinstance(text, str) or not text for text in batch):
            raise ValueError("embedding input text must be a nonempty string")
        async with self._semaphore:
            loaded = self._loaded
            if loaded is None:
                raise RuntimeError("embedding provider must be initialized explicitly")
            return await _settled_thread(lambda: self._embed_sync(loaded, batch))

    def _embed_sync(
        self, loaded: _Loaded, texts: tuple[str, ...]
    ) -> tuple[tuple[float, ...], ...]:
        tokens = loaded.tokenizer(
            list(texts),
            padding=True,
            truncation=False,
            add_special_tokens=True,
            return_tensors="pt",
        )
        mask = tokens["attention_mask"]
        lengths = mask.sum(1)
        for length in lengths.tolist():
            if length > self._profile.max_input_tokens:
                raise EmbeddingInputLimitError(
                    actual_tokens=length, max_tokens=self._profile.max_input_tokens
                )
        with (
            _settle_cuda(loaded.torch, loaded.identity.device),
            loaded.torch.inference_mode(),
        ):
            tokens = {
                name: value.to(loaded.identity.device) for name, value in tokens.items()
            }
            mask = tokens["attention_mask"]
            output = loaded.model(**tokens)
            hidden = getattr(output, "last_hidden_state", None)
            if hidden is None or getattr(hidden, "shape", None) != (
                *mask.shape,
                QWEN_DIMENSION,
            ):
                raise EmbeddingOutputError(
                    "embedding model returned invalid hidden state"
                )
            if mask[:, -1].sum() == mask.shape[0]:
                pooled = hidden[:, -1]
            else:
                indices = mask.sum(1) - 1
                pooled = hidden[
                    loaded.torch.arange(mask.shape[0], device=loaded.identity.device),
                    indices,
                ]
            normalized = loaded.torch.nn.functional.normalize(
                pooled.float(), p=2, dim=1
            )
            rows = normalized.cpu().tolist()
        return validate_embedding_output(
            rows, expected_count=len(texts), dimension=loaded.identity.dimension
        )
