"""全局配置：API key 读取、端点、价目表。

key 读取顺序（计划 §7）：
  1. 环境变量 INFERERA_API_KEY
  2. 手上有的/api-keys/aihubmix.txt（去首尾空白）
  3. 都没有 -> 报错退出
任何日志/报告/异常里都不打印 key，只打印前 4 位 + 长度。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 项目根 = 本文件的上一级的上一级（getterms/config.py -> getterms -> 术语提取）
ROOT = Path(__file__).resolve().parent.parent

# 语料目录按语种分开放。**不要把不同语种的 zip 混进同一个目录** ——
# 分层键、方向判定、提示词、缓存目录全都按语种走，混放会让审计与取样失真。
CORPUS_DIRS = {
    "es": ROOT / "手上有的" / "语料",
    "fr": ROOT / "手上有的" / "法语全量语料",
    # 俄语全量语料 2026-09-18 到手：9 个 zip / 533 文件 / 58,416 句 / 57,469 可用。
    # `ru` 现在指全量；原来那 10 个散装测试文件挪到 `ru_test`，只给 tests_ru 用。
    # **别把测试与全量混放同一个目录** —— 混放会让 audit 的层与规模统计失真，
    # 也会让 bake-off 的分层取样跑偏。
    "ru": ROOT / "手上有的" / "俄语全量语料",
    "ru_full": ROOT / "手上有的" / "俄语全量语料",   # 旧名保留，与 ru 同指
    "ru_test": ROOT / "手上有的" / "俄语测试语料",   # 10 个散装 json，1,069 句
}
CORPUS_DIR = CORPUS_DIRS["es"]        # 向后兼容：不带 lang 的老调用默认西语
KEY_DIR = ROOT / "手上有的" / "api-keys"
KEY_FILE = KEY_DIR / "aihubmix.txt"          # 中转站，向后兼容
PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
CACHE_DIR = ROOT / "cache"
OUT_DIR = ROOT / "out"
BAKEOFF_DIR = ROOT / "bakeoff"
LOG_DIR = ROOT / "logs"

BASE_URL = "https://api.inferera.com/v1"      # 中转站，向后兼容

# 三语**定稿 dump**：质检工具的默认目标，单一出处。
# ⚠ 为什么必须集中登记：`span_defects.py` 原来把默认目标硬编码成**无版本后缀的
#   v0 基线**，不带参数跑会「成功」并打出一张关于**旧版本**的表，没有任何提示。
#   2026-09-18 实测到。工具默认值指向哪个版本，是必须显式维护的事实。
# ⚠ 另一半地雷（见 selfcheck.py docstring 第 1 条）：v5 及更早的 dump 里
#   `term_src`/`term_tgt` 是**原句切片**，v6 起是**交付值（词典形）**。
#   拿这三份互相比、或与更早的比，必须先说清量的是哪一列。
# ⚠ **改提示词版本时必须同时改这里。** 2026-09-18 踩过两次：这三行落后一整代
#   （提示词已经前移到 v7，这里还指着 v6），而不带参数跑 selfcheck / span_defects
#   会「成功」并打出**上一代**的表，不报任何警 —— 于是「改完提示词跑一下质检」
#   得到的是改动前的结论。指的是**第 1 臂**（同一提示词的 n=2 采样，第 2 臂带 __r2）。
FINAL_DUMPS = {
    "es": BAKEOFF_DIR / "terms_gemini-3_7-flash__high__b10__es_v8__g3.json",
    "fr": BAKEOFF_DIR / "terms_gemini-3_7-flash__high__b10__fr__fr_v7__g2.json",
    "ru": BAKEOFF_DIR / "terms_gemini-3_7-flash__high__b10__ru__pf35__ru_v6__full_g2.json",
}

# HTML 工具定死的 10 类，无 num（计划 §1）
ALLOWED_TYPES = (
    "other", "hot", "cul", "tech", "poli", "econ", "tab", "per", "loc", "org",
)

# 价目表：美元 / 每 M token。键 = profiles.py 里的注册名。
# 中转站价格来源：aihubmix 模型页（计划 §4.2）
# 直连价格来源：各厂官方定价页（2026-09 实查，见 §5）
PRICING = {
    # ---- 中转站 inferera / aihubmix
    "gemini-3.7-flash":          {"in": 0.75,   "out": 3.75,   "cached": 0.075},
    "gemini-3.8-flash":          {"in": 0.75,   "out": 3.75,   "cached": 0.075},
    "gemini-3.7-flash-free":     {"in": 0.0,    "out": 0.0,    "cached": 0.0},
    "gemini-3.8-flash-free":     {"in": 0.0,    "out": 0.0,    "cached": 0.0},
    "qwen3.8-max-2026-09-02":    {"in": 1.69,   "out": 5.07,   "cached": 0.169},
    "glm-5.3":                   {"in": 1.1268, "out": 3.9438, "cached": 0.2817},
    # ---- 官方直连（走各厂自己的端点，价格更低且档位真的生效）
    "bailian/qwen3.8-max-0902":  {"in": 1.69,   "out": 5.07,   "cached": 0.338},
    "bailian/qwen3.8-max":       {"in": 1.69,   "out": 5.07,   "cached": 0.338},
    "zhipu/glm-5.3":             {"in": 1.252,  "out": 4.382,  "cached": 0.313},
    # 百炼上的第三方 GLM：暂按智谱同价记账，实跑后按阿里云账单校准
    "bailian/glm-5.3":           {"in": 1.252,  "out": 4.382,  "cached": 0.313},
    # DeepSeek-V4.1-Flash 峰时价；谷时是一半（$0.15/$0.60/$0.003）
    "deepseek/deepseek-flash":   {"in": 0.30,   "out": 1.20,   "cached": 0.006},
}


def utf8_stdout() -> None:
    """把 stdout 切成 UTF-8。

    Windows 控制台默认 GBK，打西语 `ñ`、法语 `ç`、俄语西里尔和 `✗` 都会抛
    UnicodeEncodeError —— 报告已经算完了，却因为一个字符整份丢掉（2026-09-18 实测）。
    报告是给人看的，宁可某个字符显示成 `?` 也不能丢整份。

    ⚠ **stderr 也要修**：2026-09-18 实测，bakeoff 的「拒绝开跑」闸门用中文写在
      SystemExit 里，GBK 编不出来 —— 程序确实以 exit 1 拦住了，**但拦截原因一个字
      都没打出来**，看起来像是无声退出。一个读不到理由的拒绝比不拒绝更糟。
    """
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


class ConfigError(RuntimeError):
    pass


def corpus_dir(lang: str) -> Path:
    """取该语种的语料目录。目录不存在也照样返回，由调用方报「0 个 zip」。"""
    d = CORPUS_DIRS.get(lang)
    if d is None:
        raise ConfigError(
            f"未登记的语种 {lang!r}。已登记: {sorted(CORPUS_DIRS)}。"
            "新增语种请在 config.CORPUS_DIRS 里加一行，并先跑 getterms.audit 做模式审计。"
        )
    return d


def load_api_key(key_file: str = "aihubmix.txt") -> str:
    """取 key。顺序：对应的环境变量 -> 手上有的/api-keys/<key_file> -> 报错。

    调用方不得打印返回值，日志里只用 key_fingerprint()。
    """
    env_name = {
        "aihubmix.txt": "INFERERA_API_KEY",
        "bailian.txt": "DASHSCOPE_API_KEY",
        "glm.txt": "ZHIPU_API_KEY",
        "deepseek.txt": "DEEPSEEK_API_KEY",
    }.get(key_file)
    if env_name:
        env = os.environ.get(env_name, "").strip()
        if env:
            return env
    p = KEY_DIR / key_file
    if p.exists():
        key = p.read_text(encoding="utf-8-sig").strip()
        if key:
            return key
        raise ConfigError(f"key 文件存在但内容为空: {p}")
    raise ConfigError(
        f"找不到 API key。请设环境变量 {env_name or '(无)'}，或把 key 写进 {p}"
    )


def key_fingerprint(key: str) -> str:
    """给日志用的安全指纹：前 4 位 + 长度。绝不返回完整 key。"""
    return f"{key[:4]}...(len={len(key)})"


def cost_usd(model: str, prompt_tokens: int, cached_tokens: int,
             completion_tokens: int) -> float:
    """按价目表折算成本。cached_tokens 是 prompt_tokens 的子集（缓存命中部分）。

    completion_tokens 已含思考 token（OpenAI 兼容口的惯例），按输出价计。
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
