"""Minimal Ollama client over stdlib urllib.

Deliberately not the `ollama` pip package: embeddings and inference both run in
the Ollama process that is already on this machine, which keeps this project's
dependency footprint at a single wheel (sqlite-vec).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request


class OllamaError(RuntimeError):
    pass


class Ollama:
    def __init__(self, url: str, chat_model: str, embed_model: str):
        self.url = url.rstrip("/")
        self.chat_model = chat_model
        self.embed_model = embed_model
        self.generate_calls = 0
        self.embed_calls = 0

    def _post(self, path: str, payload: dict, timeout: float) -> dict:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.url}{path}", data=data, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise OllamaError(f"{path} failed: HTTP {exc.code} {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OllamaError(
                f"cannot reach Ollama at {self.url} ({exc}). Is `ollama serve` running?"
            ) from exc

    def tags(self, timeout: float = 5.0) -> list[str]:
        req = urllib.request.Request(f"{self.url}/api/tags")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read())
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OllamaError(
                f"cannot reach Ollama at {self.url} ({exc}). Is `ollama serve` running?"
            ) from exc
        return [m.get("name", "") for m in body.get("models", [])]

    def health(self) -> list[str]:
        """Return a list of human-readable problems; empty means healthy."""
        try:
            installed = self.tags()
        except OllamaError as exc:
            return [str(exc)]
        problems = []
        for model in (self.chat_model, self.embed_model):
            # Ollama reports names as `qwen3:8b`; a bare `qwen3` implies `:latest`.
            wanted = model if ":" in model else f"{model}:latest"
            if wanted not in installed and model not in installed:
                problems.append(f"model {model!r} not installed -- run: ollama pull {model}")
        return problems

    def generate(self, prompt: str, *, json_mode: bool = True,
                 temperature: float = 0.2, num_predict: int = 800,
                 timeout: float = 300.0) -> str:
        payload = {
            "model": self.chat_model,
            "prompt": prompt,
            "stream": False,
            # Qwen3 emits <think> traces by default, which break JSON parsing.
            "think": False,
            "options": {"temperature": temperature, "num_predict": num_predict},
        }
        if json_mode:
            payload["format"] = "json"
        self.generate_calls += 1
        body = self._post("/api/generate", payload, timeout)
        return body.get("response", "")

    def embed(self, texts: list[str], *, timeout: float = 300.0) -> list[list[float]]:
        if not texts:
            return []
        self.embed_calls += 1
        body = self._post(
            "/api/embed", {"model": self.embed_model, "input": texts}, timeout
        )
        vectors = body.get("embeddings")
        if not vectors or len(vectors) != len(texts):
            raise OllamaError(
                f"embed returned {len(vectors or [])} vectors for {len(texts)} inputs; "
                f"is {self.embed_model!r} an embedding model?"
            )
        return vectors
