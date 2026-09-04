"""Optional summarizer backend: the local `claude` CLI.

Sharper than a small local model, but it costs tokens and needs network, so it
is opt-in via `--summarizer claude`.

The flags matter. A default `claude -p` invocation in a configured environment
loads every setting, plugin, skill and MCP server -- measured at ~21k input
tokens for a trivial prompt. Trimming the context roughly halves that. `--bare`
would trim more but forces ANTHROPIC_API_KEY and ignores OAuth, which breaks
subscription auth, so it is deliberately not used.
"""

from __future__ import annotations

import json
import subprocess
import tempfile

from .ollama import OllamaError

SYSTEM = "You summarize developer coding sessions. Reply with JSON only."


class ClaudeCLI:
    """Drop-in stand-in for Ollama.generate(); embeddings stay local."""

    def __init__(self, model: str = "claude-haiku-4-5-20251001", timeout: float = 300.0):
        self.model = model
        self.timeout = timeout
        self.generate_calls = 0

    def health(self) -> list[str]:
        try:
            proc = subprocess.run(
                ["claude", "--version"], capture_output=True, text=True, timeout=30
            )
        except FileNotFoundError:
            return ["`claude` CLI not found on PATH"]
        except (OSError, subprocess.TimeoutExpired) as exc:
            return [f"`claude --version` failed: {exc}"]
        if proc.returncode != 0:
            return [f"`claude --version` exited {proc.returncode}"]
        return []

    def generate(self, prompt: str, *, json_mode: bool = True,
                 temperature: float = 0.2, num_predict: int = 800,
                 timeout: float | None = None) -> str:
        self.generate_calls += 1
        # Run somewhere neutral so no project CLAUDE.md is picked up.
        with tempfile.TemporaryDirectory() as workdir:
            try:
                proc = subprocess.run(
                    [
                        "claude", "-p", prompt,
                        "--model", self.model,
                        "--system-prompt", SYSTEM,
                        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                        "--disable-slash-commands",
                        "--restricted",
                        "--output-format", "json",
                    ],
                    capture_output=True, text=True, cwd=workdir,
                    timeout=timeout or self.timeout,
                )
            except FileNotFoundError as exc:
                raise OllamaError("`claude` CLI not found on PATH") from exc
            except subprocess.TimeoutExpired as exc:
                raise OllamaError(f"`claude` timed out after {self.timeout:.0f}s") from exc

        if proc.returncode != 0:
            raise OllamaError(
                f"`claude` exited {proc.returncode}: {(proc.stderr or '')[:300]}"
            )
        try:
            envelope = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise OllamaError("could not parse `claude` output as JSON") from exc
        return envelope.get("result") or ""
