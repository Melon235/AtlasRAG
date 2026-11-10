"""Offline checks of the actual provider, substituting only optional SDKs."""

from __future__ import annotations

import asyncio
import importlib
import math
import subprocess
import sys
import threading
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from pydantic import ValidationError

REVISION = "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"
MODEL_ID = "Qwen/Qwen3-Embedding-0.6B"


class Tensor(np.ndarray[Any, Any]):
    def to(self, device: object) -> Tensor:
        return self

    def float(self) -> Tensor:
        return self

    def cpu(self) -> Tensor:
        return self


def tensor(value: object) -> Tensor:
    return np.asarray(value).view(Tensor)


class SDK:
    def __init__(self) -> None:
        self.loads: list[tuple[str, str, dict[str, Any]]] = []
        self.downloads: list[dict[str, Any]] = []
        self.inputs: list[list[str]] = []
        self.token_options: list[dict[str, Any]] = []
        self.model_calls = 0
        self.padding_side = "left"
        self.available = True
        self.error: BaseException | None = None
        self.output: object = None
        self.started = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.load_started = threading.Event()
        self.load_release = threading.Event()
        self.load_release.set()
        self.device = "cuda:0"
        self.dtype = "float16"
        self.dimension = 1024
        self.context = 32768
        self.sync_started = threading.Event()
        self.sync_release = threading.Event()
        self.sync_release.set()
        self.sync_error: BaseException | None = None
        self.snapshot = f"/fake/models--Qwen--Qwen3-Embedding-0.6B/snapshots/{REVISION}"

    def snapshot_download(self, **kwargs: Any) -> str:
        self.downloads.append(kwargs)
        return self.snapshot

    def synchronize(self, device: object) -> None:
        self.sync_started.set()
        assert self.sync_release.wait(10)
        if self.sync_error is not None:
            raise self.sync_error

    def load_tokenizer(self, path: str, **kwargs: Any) -> SDK:
        self.loads.append(("tokenizer", path, kwargs))
        return self

    def load_model(self, path: str, **kwargs: Any) -> Any:
        self.loads.append(("model", path, kwargs))
        self.load_started.set()
        assert self.load_release.wait(10)
        return SimpleNamespace(
            config=SimpleNamespace(
                hidden_size=self.dimension, max_position_embeddings=self.context
            ),
            to=self.move_model,
        )

    def move_model(self, device: object) -> Any:
        self.device = str(device)
        return SimpleNamespace(
            eval=lambda: self,
        )

    def parameters(self) -> Any:
        yield SimpleNamespace(device=self.device, dtype=self.dtype)

    @property
    def config(self) -> Any:
        return SimpleNamespace(
            hidden_size=self.dimension, max_position_embeddings=self.context
        )

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if args:
            texts = list(args[0])
            self.inputs.append(texts)
            self.token_options.append(kwargs)
            rows = [[ord(c) for c in text] + [1] for text in texts]
            width = max(map(len, rows))
            padded = []
            masks = []
            for row in rows:
                pads = [0] * (width - len(row))
                padded.append(pads + row if self.padding_side == "left" else row + pads)
                mask = [1] * len(row)
                masks.append(
                    pads + mask if self.padding_side == "left" else mask + pads
                )
            return {"input_ids": tensor(padded), "attention_mask": tensor(masks)}
        self.model_calls += 1
        self.started.set()
        assert self.release.wait(10)
        if self.error is not None:
            raise self.error
        if self.output is not None:
            return self.output
        ids = kwargs["input_ids"]
        hidden = np.zeros((*ids.shape, self.dimension))
        hidden[:, :, 0] = ids
        hidden[:, :, 1] = ids.sum(axis=1)[:, None]
        return SimpleNamespace(last_hidden_state=tensor(hidden))


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch) -> SDK:
    result = SDK()
    torch = SimpleNamespace(
        cuda=SimpleNamespace(
            is_available=lambda: result.available,
            current_device=lambda: 0,
            synchronize=result.synchronize,
        ),
        device=lambda value: "cuda:0" if value == "cuda" else value,
        float16="float16",
        float32="float32",
        inference_mode=nullcontext,
        arange=lambda size, **kw: np.arange(size),
        nn=SimpleNamespace(
            functional=SimpleNamespace(
                normalize=lambda value, **kw: tensor(
                    value / np.linalg.norm(value, axis=1, keepdims=True)
                )
            )
        ),
    )
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(
        sys.modules,
        "transformers",
        SimpleNamespace(
            AutoTokenizer=SimpleNamespace(from_pretrained=result.load_tokenizer),
            AutoModel=SimpleNamespace(from_pretrained=result.load_model),
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(snapshot_download=result.snapshot_download),
    )
    return result


def provider(*, batch_size: int = 1, device: str = "cuda", **kwargs: Any) -> Any:
    qwen = importlib.import_module("atlasrag.providers.embedding.qwen")
    profile = importlib.import_module("atlasrag.providers.embedding.profile")
    return qwen.QwenEmbeddingProvider(
        device=device,
        profile=profile.EmbeddingExecutionProfile(batch_size=batch_size, **kwargs),
    )


def test_provider_module_exists() -> None:
    root = Path(__file__).resolve().parents[2]
    assert (root / "src/atlasrag/providers/embedding/qwen.py").is_file()


def test_import_and_construction_do_not_import_optional_sdks() -> None:
    code = """
import sys
from atlasrag.providers.embedding.qwen import QwenEmbeddingProvider
QwenEmbeddingProvider(device='cuda')
assert not {'torch', 'transformers', 'huggingface_hub'} & sys.modules.keys()
"""
    subprocess.run([sys.executable, "-c", code], check=True)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"batch_size": True},
        {"batch_size": 0},
        {"batch_size": "1"},
        {"max_input_tokens": 0},
        {"max_input_tokens": 32769},
        {"max_input_tokens": 1.5},
        {"unknown": 1},
    ],
)
def test_profile_is_strict(kwargs: dict[str, object]) -> None:
    module = importlib.import_module("atlasrag.providers.embedding.profile")
    with pytest.raises(ValidationError):
        module.EmbeddingExecutionProfile(**kwargs)


def test_profile_is_immutable() -> None:
    module = importlib.import_module("atlasrag.providers.embedding.profile")
    profile = module.EmbeddingExecutionProfile()
    assert (profile.batch_size, profile.max_input_tokens) == (1, 8192)
    with pytest.raises(ValidationError):
        profile.batch_size = 2


@pytest.mark.parametrize(
    "device", ["auto", "mps", "cuda:-1", "cuda:0junk", "cpu:0", "", " cuda", True]
)
def test_device_requires_explicit_cpu_or_cuda(device: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        provider(device=device)


@pytest.mark.asyncio
async def test_explicit_initialization_pins_resolved_identity_and_loads_once(
    sdk: SDK,
) -> None:
    p = provider()
    with pytest.raises(RuntimeError):
        _ = p.identity
    with pytest.raises(RuntimeError):
        await p.embed(["a"])
    await asyncio.gather(p.initialize(), p.initialize())
    assert len(sdk.loads) == 2
    assert sdk.downloads == [
        {
            "repo_id": MODEL_ID,
            "revision": REVISION,
            "local_files_only": True,
            "allow_patterns": [
                "config.json",
                "model.safetensors",
                "tokenizer.json",
                "tokenizer_config.json",
                "vocab.json",
                "merges.txt",
                "special_tokens_map.json",
                "added_tokens.json",
            ],
        }
    ]
    assert all(
        path == sdk.snapshot
        and opts["local_files_only"] is True
        and opts["trust_remote_code"] is False
        for _, path, opts in sdk.loads
    )
    assert sdk.loads[1][2]["dtype"] == "float16"
    assert sdk.loads[1][2]["attn_implementation"] == "sdpa"
    assert p.identity.model_id == p.identity.tokenizer_id == MODEL_ID
    assert p.identity.model_revision == p.identity.tokenizer_revision == REVISION
    assert p.identity.dimension == 1024
    assert p.identity.device == "cuda:0"


@pytest.mark.asyncio
async def test_cpu_is_explicit_float32_and_download_requires_opt_in(sdk: SDK) -> None:
    sdk.dtype = "float32"
    p = provider(device="cpu")
    await p.initialize(allow_download=True)
    assert sdk.loads[1][2]["dtype"] == "float32"
    assert sdk.downloads[0]["local_files_only"] is False
    assert p.identity.device == "cpu"


@pytest.mark.asyncio
async def test_cuda_unavailable_fails_without_loading_or_fallback(sdk: SDK) -> None:
    sdk.available = False
    with pytest.raises(RuntimeError, match="CUDA"):
        await provider().initialize()
    assert not sdk.loads and not sdk.downloads


@pytest.mark.parametrize(
    "field,value",
    [
        ("dimension", 512),
        ("context", 8192),
        ("dtype", "float32"),
        ("snapshot", "/fake/snapshots/main"),
    ],
)
@pytest.mark.asyncio
async def test_loaded_configuration_must_match(
    sdk: SDK, field: str, value: object
) -> None:
    setattr(sdk, field, value)
    with pytest.raises(RuntimeError):
        await provider().initialize()


@pytest.mark.parametrize("padding", ["left", "right"])
@pytest.mark.asyncio
async def test_preserves_whitespace_order_and_last_token_pooling(
    sdk: SDK, padding: str
) -> None:
    sdk.padding_side = padding
    p = provider(batch_size=3)
    await p.initialize()
    texts = ["a", "bb", " " * 1500]
    vectors = await p.embed(texts)
    assert sdk.inputs == [texts]
    assert sdk.token_options[0] == {
        "padding": True,
        "truncation": False,
        "add_special_tokens": True,
        "return_tensors": "pt",
    }
    assert isinstance(vectors, tuple) and all(isinstance(v, tuple) for v in vectors)
    for text, row in zip(texts, vectors, strict=True):
        total = sum(map(ord, text)) + 1
        assert row[:2] == pytest.approx(
            (1 / math.hypot(1, total), total / math.hypot(1, total))
        )
        assert len(row) == 1024
        assert math.sqrt(sum(x * x for x in row)) == pytest.approx(1)


@pytest.mark.asyncio
async def test_token_limit_includes_special_tokens_before_inference(sdk: SDK) -> None:
    failures = importlib.import_module("atlasrag.domain.indexing.failures")
    p = provider()
    await p.initialize()
    await p.embed(["a" * 8191])
    assert sdk.model_calls == 1
    with pytest.raises(failures.EmbeddingInputLimitError) as error:
        await p.embed(["a" * 8192])
    assert (error.value.actual_tokens, error.value.max_tokens) == (8193, 8192)
    assert sdk.model_calls == 1


@pytest.mark.parametrize("texts", [[""], [], ["a", "b"], "a", {"a"}, [1]])
@pytest.mark.asyncio
async def test_invalid_input_and_unbounded_batches_rejected(
    sdk: SDK, texts: Any
) -> None:
    p = provider()
    await p.initialize()
    with pytest.raises((TypeError, ValueError)):
        await p.embed(texts)
    assert not sdk.inputs


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [[1.0]],
        [[1.0, 2.0], [3.0, 4.0]],
        [{1.0, 2.0}],
        [[float("nan"), 0]],
        [[float("inf"), 0]],
        [[True, 1]],
        [["1", 1]],
        {"x": [1, 2]},
    ],
)
def test_generic_boundary_rejects_malformed_output(raw: object) -> None:
    base = importlib.import_module("atlasrag.providers.embedding.base")
    failures = importlib.import_module("atlasrag.domain.indexing.failures")
    with pytest.raises(failures.EmbeddingOutputError):
        base.validate_embedding_output(raw, expected_count=1, dimension=2)


def test_generic_boundary_freezes_without_reordering() -> None:
    base = importlib.import_module("atlasrag.providers.embedding.base")
    rows = [[3, 4], [1, 2]]
    assert base.validate_embedding_output(rows, expected_count=2, dimension=2) == (
        (3.0, 4.0),
        (1.0, 2.0),
    )


@pytest.mark.parametrize(
    "error",
    [
        ValueError("unknown"),
        Exception("unknown"),
        MemoryError("OOM"),
        TimeoutError("timeout"),
        ImportError("missing"),
    ],
)
@pytest.mark.asyncio
async def test_technical_errors_propagate_unchanged(
    sdk: SDK, error: BaseException
) -> None:
    p = provider()
    await p.initialize()
    sdk.error = error
    with pytest.raises(type(error)) as caught:
        await p.embed(["a"])
    assert caught.value is error


@pytest.mark.parametrize(
    "output",
    [
        SimpleNamespace(),
        SimpleNamespace(last_hidden_state=None),
        SimpleNamespace(last_hidden_state=[]),
        SimpleNamespace(last_hidden_state=tensor(np.zeros((1, 2, 512)))),
        SimpleNamespace(last_hidden_state=tensor(np.full((1, 2, 1024), float("nan")))),
    ],
)
@pytest.mark.asyncio
async def test_malformed_successful_model_output_is_controlled(
    sdk: SDK, output: object
) -> None:
    failures = importlib.import_module("atlasrag.domain.indexing.failures")
    p = provider()
    await p.initialize()
    sdk.output = output
    with pytest.raises(failures.EmbeddingOutputError):
        await p.embed(["a"])


async def wait_event(event: threading.Event) -> None:
    for _ in range(1000):
        if event.is_set():
            return
        await asyncio.sleep(0.001)
    pytest.fail("worker did not start")


@pytest.mark.parametrize(
    "count,dimension",
    [(True, 2), (1, True), (0, 2), (1, 0), (-1, 2), (1, -1), (1.0, 2), (1, "2")],
)
def test_output_boundary_rejects_invalid_validation_configuration(
    count: object, dimension: object
) -> None:
    base = importlib.import_module("atlasrag.providers.embedding.base")
    failures = importlib.import_module("atlasrag.domain.indexing.failures")
    with pytest.raises(ValueError) as error:
        base.validate_embedding_output(
            [[1, 2]], expected_count=count, dimension=dimension
        )
    assert not isinstance(error.value, failures.EmbeddingOutputError)


@pytest.mark.asyncio
async def test_error_and_repeated_cancel_wait_for_issued_cuda_work(sdk: SDK) -> None:
    p = provider()
    await p.initialize()
    sdk.sync_started.clear()
    sdk.sync_release.clear()
    sdk.error = ValueError("forward failed after issuing kernels")
    first = asyncio.create_task(p.embed(["a"]))
    try:
        await wait_event(sdk.started)
        await asyncio.sleep(0.02)
        assert sdk.sync_started.is_set()
        assert not first.done()
        first.cancel()
        await asyncio.sleep(0.01)
        first.cancel()
        second = asyncio.create_task(p.embed(["b"]))
        await asyncio.sleep(0.01)
        assert not first.done() and not second.done()
        assert sdk.model_calls == 1
    finally:
        sdk.sync_release.set()
        if first.done():
            first.exception()
    with pytest.raises(asyncio.CancelledError):
        await first
    with pytest.raises(ValueError):
        await second


@pytest.mark.asyncio
async def test_cuda_synchronization_failure_is_technical(sdk: SDK) -> None:
    p = provider()
    await p.initialize()
    failure = RuntimeError("CUDA asynchronous failure")
    sdk.sync_error = failure
    with pytest.raises(RuntimeError) as error:
        await p.embed(["a"])
    assert error.value is failure


@pytest.mark.parametrize("initializing", [False, True])
@pytest.mark.asyncio
async def test_repeated_cancellation_settles_work_before_releasing_semaphore(
    sdk: SDK, initializing: bool
) -> None:
    p = provider()
    if initializing:
        sdk.load_release.clear()
        first = asyncio.create_task(p.initialize())
        await wait_event(sdk.load_started)
        second = asyncio.create_task(p.initialize())
        release = sdk.load_release
    else:
        await p.initialize()
        sdk.release.clear()
        first = asyncio.create_task(p.embed(["a"]))
        await wait_event(sdk.started)
        second = asyncio.create_task(p.embed(["b"]))
        release = sdk.release
    try:
        first.cancel()
        await asyncio.sleep(0.01)
        first.cancel()
        await asyncio.sleep(0.01)
        assert not first.done() and not second.done()
        assert len(sdk.loads) == 2
        assert sdk.model_calls == (0 if initializing else 1)
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    assert len(sdk.loads) == 2
    assert sdk.model_calls == (0 if initializing else 2)
