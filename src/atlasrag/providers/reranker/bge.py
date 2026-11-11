"""Pinned local BAAI/bge-reranker-v2-m3 CrossEncoder candidate."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar, cast

from atlasrag.providers.reranker.base import (
    RerankerInputLimitError,
    RerankerModelIdentity,
    RerankerUnavailableError,
)

BGE_RERANKER_MODEL_ID = "BAAI/bge-reranker-v2-m3"
BGE_RERANKER_REVISION = "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"
BGE_RERANKER_ASSETS = (
    "config.json",
    "model.safetensors",
    "sentencepiece.bpe.model",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
)
_T = TypeVar("_T")


@contextmanager
def _settle_cuda(torch: Any, device: str) -> Iterator[None]:
    try:
        yield
    finally:
        if device.startswith("cuda"):
            torch.cuda.synchronize(device)


async def _settled_thread(operation: Callable[[], _T]) -> _T:
    def run() -> tuple[_T | None, BaseException | None]:
        try:
            return operation(), None
        except BaseException as error:
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
                f"settled reranker worker failed: {type(failure).__name__}"
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
    identity: RerankerModelIdentity


class BgeRerankerProvider:
    """One explicitly loaded FP16 CUDA CrossEncoder; no silent CPU fallback."""

    def __init__(self, *, device: str, max_input_tokens: int = 8192) -> None:
        if not isinstance(device, str) or not device.startswith("cuda"):
            raise ValueError("reranker device must be an explicit CUDA device")
        if type(max_input_tokens) is not int or max_input_tokens < 1:
            raise ValueError("max_input_tokens must be positive")
        self._device = device
        self._max_tokens = max_input_tokens
        self._semaphore = asyncio.Semaphore(1)
        self._loaded: _Loaded | None = None

    @property
    def identity(self) -> RerankerModelIdentity:
        if self._loaded is None:
            raise RuntimeError("reranker provider must be initialized explicitly")
        return self._loaded.identity

    async def initialize(self, *, allow_download: bool = False) -> None:
        if type(allow_download) is not bool:
            raise TypeError("allow_download must be a boolean")
        async with self._semaphore:
            if self._loaded is None:
                await _settled_thread(lambda: self._initialize_sync(allow_download))

    def _initialize_sync(self, allow_download: bool) -> None:
        import torch  # type: ignore[import-not-found]
        from huggingface_hub import snapshot_download  # type: ignore[import-not-found]
        from transformers import (  # type: ignore[import-not-found]
            AutoModelForSequenceClassification,
            AutoTokenizer,
        )

        if not torch.cuda.is_available():
            raise RuntimeError("requested CUDA reranker device is unavailable")
        requested = self._device
        if requested == "cuda":
            requested = f"cuda:{torch.cuda.current_device()}"
        device = torch.device(requested)
        snapshot = snapshot_download(
            repo_id=BGE_RERANKER_MODEL_ID,
            revision=BGE_RERANKER_REVISION,
            local_files_only=not allow_download,
            allow_patterns=BGE_RERANKER_ASSETS,
        )
        path = Path(snapshot)
        if path.name != BGE_RERANKER_REVISION or path.parent.name != "snapshots":
            raise RuntimeError("reranker snapshot did not resolve to pinned revision")
        tokenizer = AutoTokenizer.from_pretrained(
            snapshot,
            local_files_only=True,
            trust_remote_code=False,
        )
        with _settle_cuda(torch, str(device)):
            model = (
                AutoModelForSequenceClassification.from_pretrained(
                    snapshot,
                    local_files_only=True,
                    trust_remote_code=False,
                    dtype=torch.float16,
                    attn_implementation="sdpa",
                )
                .to(device)
                .eval()
            )
            parameters = tuple(model.parameters())
            if not parameters or any(
                str(parameter.device) != str(device) or parameter.dtype != torch.float16
                for parameter in parameters
            ):
                raise RuntimeError("reranker loaded device or dtype differs")
            if int(model.config.num_labels) != 1:
                raise RuntimeError("reranker model is not a single-logit CrossEncoder")
            identity = RerankerModelIdentity(
                model_id=BGE_RERANKER_MODEL_ID,
                model_revision=path.name,
                tokenizer_id=BGE_RERANKER_MODEL_ID,
                tokenizer_revision=path.name,
                device=str(device),
                dtype="float16",
                max_input_tokens=self._max_tokens,
            )
        self._loaded = _Loaded(torch, tokenizer, model, identity)

    async def score(self, query: str, passages: Sequence[str]) -> tuple[float, ...]:
        if not isinstance(query, str) or not query:
            raise ValueError("reranker query must be nonempty text")
        if isinstance(passages, (str, bytes)) or not isinstance(passages, Sequence):
            raise TypeError("passages must be a sequence of text")
        if not passages or any(not isinstance(item, str) for item in passages):
            raise TypeError("passages must be nonempty text values")
        async with self._semaphore:
            if self._loaded is None:
                raise RuntimeError("reranker provider must be initialized explicitly")
            try:
                return await _settled_thread(
                    lambda: self._score_sync(query, tuple(passages))
                )
            except RerankerInputLimitError:
                raise
            except Exception:
                raise RerankerUnavailableError from None

    def _score_sync(self, query: str, passages: tuple[str, ...]) -> tuple[float, ...]:
        loaded = self._loaded
        if loaded is None:
            raise RuntimeError("reranker provider must be initialized explicitly")
        torch = loaded.torch
        encoded = loaded.tokenizer(
            [query] * len(passages),
            list(passages),
            padding=True,
            truncation=False,
            return_tensors="pt",
        )
        attention = encoded["attention_mask"]
        token_counts = attention.sum(dim=1)
        if int(token_counts.max()) > loaded.identity.max_input_tokens:
            raise RerankerInputLimitError
        moved = {
            key: value.to(loaded.identity.device) for key, value in encoded.items()
        }
        with _settle_cuda(torch, loaded.identity.device):
            with torch.inference_mode():
                logits = loaded.model(**moved).logits
            if tuple(logits.shape) != (len(passages), 1):
                raise RuntimeError("reranker returned an unexpected logit shape")
            scores = torch.sigmoid(logits.float()).flatten().tolist()
        rows = tuple(float(value) for value in scores)
        if any(not math.isfinite(value) for value in rows):
            raise RuntimeError("reranker returned a non-finite score")
        return rows
