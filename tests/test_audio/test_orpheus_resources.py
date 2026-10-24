"""Post-download Orpheus initialization must use a bounded CPU working set."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from novacode_cli.audio.tts_orpheus import OrpheusSpeaker


@pytest.fixture
def orpheus_runtime(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Fake native boundaries, including upstream's dangerous n_ctx=0 default."""
    llms: list[dict[str, Any]] = []
    decoders: list[dict[str, Any]] = []
    downloads: list[str] = []
    cache: dict[str, object] = {}

    def download(repo_id: str, filename: str, **kwargs: Any) -> str:
        assert repo_id
        downloads.append(filename)
        if progress := kwargs.get("tqdm_class"):
            with progress(total=100, unit="B", desc=filename) as bar:
                bar.update(100)
        return filename

    class Llama:
        def __init__(self, **kwargs: Any) -> None:
            llms.append(kwargs)

    def decoder(_path: str, **kwargs: Any) -> object:
        decoders.append(kwargs)
        return object()

    class Orpheus:
        lang_to_model: ClassVar[dict[str, str]] = {
            "en": "owner/english-Q4_K_M-GGUF",
            "de": "owner/german-Q4_K_M-GGUF",
        }

        def __init__(self, **_kwargs: object) -> None:
            # Match orpheus-cpp 0.0.3: 0 requests the full model-trained context.
            self._llm = Llama(n_ctx=0, n_threads=0)
            self._snac_session = decoder(
                "decoder", providers=["CUDAExecutionProvider", "CPUExecutionProvider"]
            )

    monkeypatch.setitem(sys.modules, "orpheus_cpp", SimpleNamespace(OrpheusCpp=Orpheus))
    monkeypatch.setitem(sys.modules, "llama_cpp", SimpleNamespace(Llama=Llama))
    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(
            hf_hub_download=download,
            try_to_load_from_cache=lambda _repo, filename: cache.get(filename),
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "onnxruntime",
        SimpleNamespace(SessionOptions=SimpleNamespace, InferenceSession=decoder),
    )
    return SimpleNamespace(llms=llms, decoders=decoders, downloads=downloads, cache=cache)


def test_orpheus_bounds_context_and_native_threads(orpheus_runtime: SimpleNamespace):
    speaker = OrpheusSpeaker()
    speaker._ensure_voice()
    llm = orpheus_runtime.llms[0]
    assert llm["n_ctx"] == 4096, "Full trained context can exhaust RAM after download"
    assert llm["n_threads"] == 2
    assert llm["n_threads_batch"] == 2
    assert llm["n_batch"] == 128
    decoder = orpheus_runtime.decoders[0]
    assert decoder["providers"] == ["CPUExecutionProvider"]
    assert decoder["sess_options"].intra_op_num_threads == 2
    assert decoder["sess_options"].inter_op_num_threads == 1
    assert speaker._model is not None
    speaker._ensure_voice()
    assert len(orpheus_runtime.llms) == 1


def test_orpheus_preserves_selected_language(orpheus_runtime: SimpleNamespace):
    OrpheusSpeaker(lang="de")._ensure_voice()
    assert orpheus_runtime.downloads == ["german-q4_k_m.gguf", "decoder_model.onnx"]


@pytest.mark.parametrize("cached", [(), ("gguf",), ("codec",), ("gguf", "codec")])
def test_orpheus_download_check_includes_both_assets_without_native_loading(
    cached: tuple[str, ...], orpheus_runtime: SimpleNamespace
):
    if "gguf" in cached:
        orpheus_runtime.cache["english-q4_k_m.gguf"] = "cached-gguf"
    if "codec" in cached:
        orpheus_runtime.cache["onnx/decoder_model.onnx"] = "cached-decoder"
    assert OrpheusSpeaker().needs_download is (len(cached) != 2)
    assert orpheus_runtime.llms == orpheus_runtime.decoders == []
