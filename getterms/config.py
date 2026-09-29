"""Global configuration: API key lookup, endpoint, pricing.

Key lookup order:
  1. the provider's environment variable
  2. a file under KEY_DIR
  3. neither -> error
Nothing logs or reports a key; use key_fingerprint() instead.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Corpus is laid out one directory per language. Keep languages in separate
# directories: layer keys, direction detection, prompts and cache all key off
# the language, so mixing them corrupts sampling and reporting.
CORPUS_ROOT = Path(os.environ.get("CORPUS_ROOT", str(ROOT / "corpus")))
CORPUS_DIRS = {
    "es": CORPUS_ROOT / "es",
    "fr": CORPUS_ROOT / "fr",
    "ru": CORPUS_ROOT / "ru",
    "ru_full": CORPUS_ROOT / "ru",       # alias, same as "ru"
    "ru_test": CORPUS_ROOT / "ru_test",
}
CORPUS_DIR = CORPUS_DIRS["es"]        # legacy default for calls without a language

KEY_DIR = Path(os.environ.get("KEY_DIR", str(ROOT / "keys")))
PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
CACHE_DIR = ROOT / "cache"
OUT_DIR = ROOT / "out"

# Any OpenAI-compatible endpoint. Override with API_BASE_URL.
BASE_URL = os.environ.get("API_BASE_URL", "https://api.openai.com/v1")

# The ten term types. `num` is deliberately not extracted.
ALLOWED_TYPES = (
    "other", "hot", "cul", "tech", "poli", "econ", "tab", "per", "loc", "org",
)

# USD per 1M tokens. Keys match the model names registered in profiles.py.
# `cached` applies to the prompt tokens served from cache (a subset of `in`).
PRICING = {
    "gemini-3.7-flash":          {"in": 0.75,   "out": 3.75,   "cached": 0.075},
    "gemini-3.8-flash":          {"in": 0.75,   "out": 3.75,   "cached": 0.075},
    "gemini-3.7-flash-free":     {"in": 0.0,    "out": 0.0,    "cached": 0.0},
    "gemini-3.8-flash-free":     {"in": 0.0,    "out": 0.0,    "cached": 0.0},
    "qwen3.8-max-2026-09-02":    {"in": 1.69,   "out": 5.07,   "cached": 0.169},
    "glm-5.3":                   {"in": 1.1268, "out": 3.9438, "cached": 0.2817},
    "bailian/qwen3.8-max-0902":  {"in": 1.69,   "out": 5.07,   "cached": 0.338},
    "bailian/qwen3.8-max":       {"in": 1.69,   "out": 5.07,   "cached": 0.338},
    "zhipu/glm-5.3":             {"in": 1.252,  "out": 4.382,  "cached": 0.313},
    "bailian/glm-5.3":           {"in": 1.252,  "out": 4.382,  "cached": 0.313},
    "deepseek/deepseek-flash":   {"in": 0.30,   "out": 1.20,   "cached": 0.006},
}


def utf8_stdout() -> None:
    """Switch stdout/stderr to UTF-8.

    Windows consoles default to a legacy codepage, so a single `ñ`, `ç` or
    Cyrillic character raises UnicodeEncodeError and loses a whole report that
    has already been computed. Rendering that character as `?` is preferable.
    stderr needs the same treatment: otherwise a rejection raised as SystemExit
    can exit with a code and print nothing at all.
    """
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


class ConfigError(RuntimeError):
    pass


def corpus_dir(lang: str) -> Path:
    """Return the corpus directory for a language (may not exist yet)."""
    d = CORPUS_DIRS.get(lang)
    if d is None:
        raise ConfigError(
            f"unregistered language {lang!r}; known: {sorted(CORPUS_DIRS)}"
        )
    return d


# Environment variable for each provider key file name.
KEY_ENV_VARS = {
    "openai.txt":   "API_KEY",
    "dashscope.txt": "DASHSCOPE_API_KEY",
    "zhipu.txt":    "ZHIPU_API_KEY",
    "deepseek.txt": "DEEPSEEK_API_KEY",
}


def load_api_key(key_file: str = "openai.txt") -> str:
    """Return the API key: environment variable first, then KEY_DIR/<key_file>.

    Callers must not print the result; log key_fingerprint() only.
    """
    env_name = KEY_ENV_VARS.get(key_file)
    if env_name:
        env = os.environ.get(env_name, "").strip()
        if env:
            return env
    p = KEY_DIR / key_file
    if p.exists():
        key = p.read_text(encoding="utf-8-sig").strip()
        if key:
            return key
        raise ConfigError(f"key file is empty: {p}")
    raise ConfigError(
        f"no API key found; set {env_name or 'the provider env var'} "
        f"or write the key to {p}"
    )


def key_fingerprint(key: str) -> str:
    """Safe log fingerprint: first 4 characters plus length. Never the key."""
    return f"{key[:4]}...(len={len(key)})"


def cost_usd(model: str, prompt_tokens: int, cached_tokens: int,
             completion_tokens: int) -> float:
    """Cost in USD. cached_tokens is the cached subset of prompt_tokens.

    completion_tokens already includes reasoning tokens (the usual convention
    on OpenAI-compatible APIs) and is billed at the output rate.
    """
    p = PRICING.get(model)
    if p is None:
        return 0.0
    fresh_in = max(0, prompt_tokens - cached_tokens)
    return (
        fresh_in / 1_000_000 * p["in"]
        + cached_tokens / 1_000_000 * p["cached"]
        + completion_tokens / 1_000_000 * p["out"]
    )
