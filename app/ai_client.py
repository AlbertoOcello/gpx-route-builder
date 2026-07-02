"""
ai_client.py — Provider-agnostic AI wrapper for GPX Route Builder.

Reads from environment / .env:
  AI_PROVIDER   = claude | gemini | openai | ollama  (default: claude)
  AI_MODEL      = model name override (optional — uses provider default if empty)
  ANTHROPIC_API_KEY
  GEMINI_API_KEY
  OPENAI_API_KEY
  OLLAMA_URL    = http://localhost:11434 (default for ollama)

Public API:
  get_provider() -> str
  get_model()    -> str
  generate(system, prompt, max_tokens) -> str
  generate_json(system, prompt, max_tokens) -> str   # ensures JSON string back
  generate_with_web_search(system, prompt, max_tokens) -> (text, search_queries)
    Claude-only: uses web_search_20260209 server-side tool.
    Other providers: plain generate with a note injected in system prompt.
"""
from __future__ import annotations

import json
import logging
import os
import re

import httpx
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

_PROVIDER: str = os.environ.get("AI_PROVIDER", "claude").lower().strip()
_EXPLICIT_MODEL: str = os.environ.get("AI_MODEL", "").strip()

_DEFAULT_MODELS: dict[str, str] = {
    "claude": "claude-sonnet-4-6",
    "gemini": "gemini-2.0-flash",
    "openai": "gpt-4o-mini",
    "ollama": "llama3",
}

_SUPPORTED = set(_DEFAULT_MODELS)


def get_provider() -> str:
    return _PROVIDER


def get_model() -> str:
    return _EXPLICIT_MODEL or _DEFAULT_MODELS.get(_PROVIDER, "claude-sonnet-4-6")


def _check_provider() -> None:
    if _PROVIDER not in _SUPPORTED:
        raise ValueError(
            f"AI_PROVIDER='{_PROVIDER}' non supportato. "
            f"Valori validi: {', '.join(sorted(_SUPPORTED))}"
        )


# ── Internal helpers per provider ─────────────────────────────────────────────

def _generate_claude(system: str, prompt: str, max_tokens: int) -> str:
    import anthropic
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise EnvironmentError(
            "ANTHROPIC_API_KEY mancante. Aggiungila a .env: ANTHROPIC_API_KEY=sk-ant-..."
        )
    client = anthropic.Anthropic(api_key=key)
    resp = client.messages.create(
        model=get_model(),
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    if resp.stop_reason == "max_tokens":
        raise RuntimeError(f"[claude] Risposta troncata (max_tokens={max_tokens}).")
    return next((b.text for b in resp.content if b.type == "text"), "")


def _generate_gemini(system: str, prompt: str, max_tokens: int) -> str:
    try:
        import google.generativeai as genai
    except ImportError:
        raise ImportError(
            "google-generativeai non installato. Esegui: pip install google-generativeai"
        )
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        raise EnvironmentError("GEMINI_API_KEY mancante. Aggiungila a .env.")
    genai.configure(api_key=key)
    model = genai.GenerativeModel(
        model_name=get_model(),
        system_instruction=system,
    )
    resp = model.generate_content(
        prompt,
        generation_config=genai.GenerationConfig(max_output_tokens=max_tokens),
    )
    return resp.text or ""


def _generate_gemini_json(system: str, prompt: str, max_tokens: int) -> str:
    try:
        import google.generativeai as genai
    except ImportError:
        raise ImportError(
            "google-generativeai non installato. Esegui: pip install google-generativeai"
        )
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        raise EnvironmentError("GEMINI_API_KEY mancante. Aggiungila a .env.")
    genai.configure(api_key=key)
    model = genai.GenerativeModel(
        model_name=get_model(),
        system_instruction=system,
    )
    resp = model.generate_content(
        prompt,
        generation_config=genai.GenerationConfig(
            max_output_tokens=max_tokens,
            response_mime_type="application/json",
        ),
    )
    return resp.text or ""


def _generate_openai(system: str, prompt: str, max_tokens: int) -> str:
    try:
        import openai
    except ImportError:
        raise ImportError("openai non installato. Esegui: pip install openai")
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise EnvironmentError("OPENAI_API_KEY mancante. Aggiungila a .env.")
    client = openai.OpenAI(api_key=key)
    resp = client.chat.completions.create(
        model=get_model(),
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    )
    return resp.choices[0].message.content or ""


def _generate_openai_json(system: str, prompt: str, max_tokens: int) -> str:
    try:
        import openai
    except ImportError:
        raise ImportError("openai non installato. Esegui: pip install openai")
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise EnvironmentError("OPENAI_API_KEY mancante. Aggiungila a .env.")
    client = openai.OpenAI(api_key=key)
    resp = client.chat.completions.create(
        model=get_model(),
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    )
    return resp.choices[0].message.content or ""


def _generate_ollama(system: str, prompt: str, max_tokens: int) -> str:
    base_url = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
    payload = {
        "model": get_model(),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "options": {"num_predict": max_tokens},
    }
    resp = httpx.post(f"{base_url}/api/chat", json=payload, timeout=120.0)
    resp.raise_for_status()
    data = resp.json()
    return data.get("message", {}).get("content", "")


def _generate_ollama_json(system: str, prompt: str, max_tokens: int) -> str:
    base_url = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
    payload = {
        "model": get_model(),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "format": "json",
        "options": {"num_predict": max_tokens},
    }
    resp = httpx.post(f"{base_url}/api/chat", json=payload, timeout=120.0)
    resp.raise_for_status()
    data = resp.json()
    return data.get("message", {}).get("content", "")


# ── Public API ─────────────────────────────────────────────────────────────────

def generate(system: str, prompt: str, max_tokens: int = 2000) -> str:
    """Send a single-turn prompt and return the text response."""
    _check_provider()
    log.debug("[ai_client] provider=%s model=%s max_tokens=%d", _PROVIDER, get_model(), max_tokens)
    if _PROVIDER == "claude":
        return _generate_claude(system, prompt, max_tokens)
    if _PROVIDER == "gemini":
        return _generate_gemini(system, prompt, max_tokens)
    if _PROVIDER == "openai":
        return _generate_openai(system, prompt, max_tokens)
    if _PROVIDER == "ollama":
        return _generate_ollama(system, prompt, max_tokens)
    raise ValueError(f"Provider non supportato: {_PROVIDER}")


def generate_json(system: str, prompt: str, max_tokens: int = 2000) -> str:
    """Like generate() but instructs the model to return only valid JSON.

    For providers with native JSON mode (openai, gemini, ollama) the mode is
    activated at the API level.  For claude, JSON is requested via system prompt
    addition (Claude reliably honours it without forced mode).

    Returns the raw JSON string; callers parse with json.loads().
    """
    _check_provider()
    _json_system = (
        system
        + "\n\nRispondi ESCLUSIVAMENTE con JSON valido, senza testo aggiuntivo, "
        "senza markdown, senza code fence."
    )
    log.debug("[ai_client] generate_json provider=%s model=%s", _PROVIDER, get_model())
    if _PROVIDER == "claude":
        return _generate_claude(_json_system, prompt, max_tokens)
    if _PROVIDER == "gemini":
        return _generate_gemini_json(_json_system, prompt, max_tokens)
    if _PROVIDER == "openai":
        return _generate_openai_json(_json_system, prompt, max_tokens)
    if _PROVIDER == "ollama":
        return _generate_ollama_json(_json_system, prompt, max_tokens)
    raise ValueError(f"Provider non supportato: {_PROVIDER}")


def generate_with_web_search(
    system: str,
    prompt: str,
    max_tokens: int = 4096,
) -> tuple[str, list[str]]:
    """Generate with optional web search.

    Claude:          uses web_search_20260209 server-side tool; returns (text, queries).
    Other providers: plain generate() — web search not available.
                     Injects a notice in the system prompt so the model knows it must
                     rely only on its training data.
    Returns: (text, search_queries)  — search_queries is [] for non-Claude providers.
    """
    _check_provider()

    if _PROVIDER == "claude":
        return _generate_claude_with_search(system, prompt, max_tokens)

    # Fallback for non-Claude providers
    fallback_system = (
        system
        + "\n\n[NOTA: la ricerca web non è disponibile per questo provider AI. "
        "Usa solo le tue conoscenze di training per rispondere.]"
    )
    log.warning(
        "[ai_client] generate_with_web_search: provider=%s non supporta web search — fallback a generate()",
        _PROVIDER,
    )
    text = generate(fallback_system, prompt, max_tokens)
    return text, []


def _generate_claude_with_search(
    system: str,
    prompt: str,
    max_tokens: int,
) -> tuple[str, list[str]]:
    import anthropic
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise EnvironmentError("ANTHROPIC_API_KEY mancante. Aggiungila a .env.")
    client = anthropic.Anthropic(api_key=key)
    response = client.messages.create(
        model=get_model(),
        max_tokens=max_tokens,
        system=system,
        tools=[{"type": "web_search_20260209", "name": "web_search"}],
        tool_choice={"type": "any"},
        messages=[{"role": "user", "content": prompt}],
    )

    if response.stop_reason == "max_tokens":
        raise RuntimeError("[claude] Risposta troncata (max_tokens raggiunto).")

    search_queries: list[str] = []
    for block in response.content:
        btype = getattr(block, "type", None)
        if btype == "server_tool_use" and getattr(block, "name", "") == "web_search":
            inp = getattr(block, "input", {}) or {}
            q = inp.get("query", "") if isinstance(inp, dict) else ""
            if q:
                search_queries.append(q)

    text = "\n".join(
        b.text for b in response.content if getattr(b, "type", None) == "text"
    )
    if not text.strip():
        raise RuntimeError(
            f"[claude] Nessun blocco text nella risposta "
            f"(stop_reason={response.stop_reason!r})."
        )
    return text, search_queries


def check_api_key() -> None:
    """Raise EnvironmentError if the required key / endpoint for the current provider is missing."""
    _check_provider()
    if _PROVIDER == "claude" and not os.environ.get("ANTHROPIC_API_KEY"):
        raise EnvironmentError(
            "ANTHROPIC_API_KEY non trovata.\n"
            "Impostala in .env: ANTHROPIC_API_KEY=sk-ant-..."
        )
    if _PROVIDER == "gemini" and not os.environ.get("GEMINI_API_KEY"):
        raise EnvironmentError("GEMINI_API_KEY non trovata. Aggiungila a .env.")
    if _PROVIDER == "openai" and not os.environ.get("OPENAI_API_KEY"):
        raise EnvironmentError("OPENAI_API_KEY non trovata. Aggiungila a .env.")
    if _PROVIDER == "ollama":
        url = os.environ.get("OLLAMA_URL", "http://localhost:11434")
        try:
            r = httpx.get(f"{url}/api/tags", timeout=3.0)
            r.raise_for_status()
        except Exception as exc:
            raise EnvironmentError(
                f"Ollama non raggiungibile su {url}: {exc}\n"
                "Avvia Ollama prima di usare l'app."
            ) from exc
