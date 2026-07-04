"""
Internationalisation — t("key.path") returns the string for the active language.

Language detection order:
  1. st.session_state["lang"]  (set by the UI selectbox)
  2. LANG env var (it_IT.* → "it", anything else → "en")
  3. Fallback: "it"
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import streamlit as st
import yaml

_YAML_PATH = Path(__file__).parent.parent / "config" / "translations.yaml"


@lru_cache(maxsize=1)
def _load() -> dict:
    with open(_YAML_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _get(data: dict, key: str, lang: str) -> str:
    parts = key.split(".")
    node = data.get(lang, {})
    for part in parts:
        if not isinstance(node, dict):
            break
        node = node.get(part, None)
        if node is None:
            break
    if isinstance(node, str):
        return node
    # Fallback to Italian if the key is missing in the requested language
    if lang != "it":
        return _get(data, key, "it")
    return key  # ultimate fallback: return the key itself


def active_lang() -> str:
    """Returns the active language code ("it" or "en")."""
    if "lang" in st.session_state:
        return st.session_state["lang"]
    env_lang = os.environ.get("LANG", "")
    return "it" if env_lang.startswith("it") else "en"


def t(key: str) -> str:
    """Translate *key* for the active language."""
    return _get(_load(), key, active_lang())


def render_language_selector() -> None:
    """Renders the compact language selector and updates session_state['lang']."""
    detected = "it" if os.environ.get("LANG", "").startswith("it") else "en"
    st.session_state.setdefault("lang", detected)

    options = ["🇮🇹 Italiano", "🇬🇧 English"]
    current_idx = 0 if st.session_state["lang"] == "it" else 1

    col_lang, _ = st.columns([1, 5])
    with col_lang:
        sel = st.selectbox(
            "🌐",
            options,
            index=current_idx,
            key="lang_selector",
            label_visibility="collapsed",
        )
    st.session_state["lang"] = "it" if "Italiano" in sel else "en"
