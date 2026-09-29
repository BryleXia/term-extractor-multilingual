"""Per-model request profiles: which parameters to send, which to omit.

`never_send` is load-bearing: sending one of those parameters is rejected or
silently ignored by the provider.
"""
from __future__ import annotations

from dataclasses import dataclass, field

@dataclass(frozen=True)
class Provider:
    """An API provider: endpoint, key file, and a safe concurrency ceiling.

    `max_concurrency` is a conservative ceiling; run.py warns and backs off if
    the effective concurrency exceeds it.
    """
    name: str
    base_url: str
    key_file: str
    max_concurrency: int = 50
    rate_limit_note: str = ""

OPENAI_COMPAT = Provider("openai", "https://api.openai.com/v1", "openai.txt",
                         max_concurrency=100,
                         rate_limit_note="no published ceiling")
DASHSCOPE = Provider("dashscope",
                     "https://dashscope.aliyuncs.com/compatible-mode/v1",
                     "dashscope.txt", max_concurrency=20,
                     rate_limit_note="tier dependent; 20 is conservative")
ZHIPU = Provider("zhipu", "https://open.bigmodel.cn/api/paas/v4", "zhipu.txt",
                 max_concurrency=2,
                 rate_limit_note="low tier; the top effort tier is effectively unusable")
DEEPSEEK = Provider("deepseek", "https://api.deepseek.com/v1", "deepseek.txt",
                    max_concurrency=100,
                    rate_limit_note="documented ceiling 2500")

@dataclass(frozen=True)
class ModelProfile:
    model: str                    # 发给 API 的 model id（各家叫法不同！）
    vendor: str
    # 该模型自己的 reasoning_effort 可用档位（不套别家档位名）
    effort_enum: tuple[str, ...]
    effort_default: str
    # 各档位的输出上限。注意官方原文：max_completion_tokens 是
    # "including visible output tokens and reasoning tokens"，思考 token 算在内，
    # 所以这里必须是「思考上限 + 可见输出预算」，不能拍 8000。
    max_out_by_effort: dict[str, int]
    max_out_param: str            # max_completion_tokens / max_tokens
    supports_json_object: bool
    supports_json_schema: bool
    send_temperature: bool
    never_send: tuple[str, ...]   # 明确不发的参数
    requires_json_keyword: bool   # response_format=json_object 时提示词必须含 "JSON"
    provider: Provider = OPENAI_COMPAT
    # DeepSeek 的 reasoning_effort 是 1~100 连续值，不是枚举
    effort_is_numeric: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def key(self) -> str:
        """Registry name: bare model id, prefixed with the provider name."""
        if self.provider is OPENAI_COMPAT:
            return self.model
        return f"{self.provider.name}/{self.model}"

    def effort_ok(self, effort: str | None) -> bool:
        if effort is None:
            return True
        if self.effort_is_numeric:
            try:
                v = int(effort)
            except (TypeError, ValueError):
                return False
            return 1 <= v <= 100
        return effort in self.effort_enum

    def build_kwargs(self, effort: str | None) -> dict:
        """按剖面产出这个模型真正该发的参数（除 model/messages 外）。"""
        kw: dict = {}
        tier = effort or self.effort_default
        if effort is not None:
            if not self.effort_ok(effort):
                raise ValueError(
                    f"{self.key} 的 reasoning_effort "
                    + (f"须是 1~100 的整数" if self.effort_is_numeric
                       else f"只接受 {self.effort_enum}")
                    + f"，收到 {effort!r}"
                )
            kw["reasoning_effort"] = int(effort) if self.effort_is_numeric else effort
        if self.effort_is_numeric:
            # 数值档位无法查表，按档位线性给上限
            v = int(tier)
            kw[self.max_out_param] = 8_000 + int(v / 100 * 56_000)
        else:
            kw[self.max_out_param] = self.max_out_by_effort[tier]
        if self.supports_json_object:
            kw["response_format"] = {"type": "json_object"}
        # temperature：Gemini 官方明文要求移除；Qwen 思考模式下 <0.6 会被强改；
        # GLM 可发但思考常开、设 0 无意义。一律不发（send_temperature 全为 False）。
        return kw

# --------------------------------------------------------------- Gemini Flash
_GEMINI_COMMON = dict(
    vendor="google",
    effort_enum=("low", "medium", "high"),
    effort_default="medium",
    # 思考上限 + 8K 可见输出预算，留一倍安全余量
    max_out_by_effort={"low": 10_000, "medium": 20_000, "high": 40_000},
    max_out_param="max_completion_tokens",   # max_tokens 已废弃
    supports_json_object=True,
    supports_json_schema=True,
    send_temperature=False,
    never_send=("temperature", "top_p", "top_k", "frequency_penalty",
                "presence_penalty", "thinking_level", "thinking_budget",
                "extra_body.google.thinking_config"),
    requires_json_keyword=False,
)

GEMINI_37 = ModelProfile(
    model="gemini-3.7-flash",
    notes=(),
    **_GEMINI_COMMON,
)

GEMINI_38 = ModelProfile(
    model="gemini-3.8-flash",
    notes=(),
    **_GEMINI_COMMON,
)

# Free variants: for pipeline smoke-testing only.
GEMINI_37_FREE = ModelProfile(model="gemini-3.7-flash-free",
                              notes=(), **_GEMINI_COMMON)
GEMINI_38_FREE = ModelProfile(model="gemini-3.8-flash-free",
                              notes=(), **_GEMINI_COMMON)

# ------------------------------------------------------------------- GLM-5.3
GLM_53 = ModelProfile(
    model="glm-5.3",
    vendor="zhipu",
    effort_enum=("low", "high", "max"),
    effort_default="max",
    max_out_by_effort={"low": 12_000, "high": 32_000, "max": 48_000},
    max_out_param="max_tokens",
    supports_json_object=True,
    supports_json_schema=False,
    send_temperature=False,
    never_send=("thinking", "top_p", "top_k", "temperature", "response_format.json_schema"),
    requires_json_keyword=False,
    notes=(),
)

# --------------------------------------------------------------- Qwen3.8-Max
QWEN_38_MAX = ModelProfile(
    model="qwen3.8-max-2026-09-02",
    vendor="alibaba",
    effort_enum=("low", "medium", "xhigh"),
    effort_default="xhigh",
    max_out_by_effort={"low": 16_000, "medium": 28_000, "xhigh": 64_000},
    max_out_param="max_completion_tokens",
    supports_json_object=True,
    supports_json_schema=False,
    send_temperature=False,
    never_send=("thinking_budget", "enable_thinking", "temperature", "top_p", "top_k"),
    requires_json_keyword=True,
    notes=(),
)

PROFILES: dict[str, ModelProfile] = {}

# ============================================================ 官方直连剖面

# Providers publish the same model under different ids.
QWEN_38_MAX_DASHSCOPE = ModelProfile(
    model="qwen3.8-max-0902",
    vendor="alibaba",
    provider=DASHSCOPE,
    effort_enum=("low", "medium", "xhigh"),
    effort_default="xhigh",
    max_out_by_effort={"low": 16_000, "medium": 28_000, "xhigh": 64_000},
    max_out_param="max_completion_tokens",
    supports_json_object=True,
    supports_json_schema=False,
    send_temperature=False,
    never_send=("thinking_budget", "enable_thinking", "temperature", "top_p", "top_k"),
    requires_json_keyword=True,
    notes=(),
)

QWEN_38_MAX_DASHSCOPE_ROLLING = ModelProfile(
    model="qwen3.8-max",
    vendor="alibaba",
    provider=DASHSCOPE,
    effort_enum=("low", "medium", "xhigh"),
    effort_default="xhigh",
    max_out_by_effort={"low": 16_000, "medium": 28_000, "xhigh": 64_000},
    max_out_param="max_completion_tokens",
    supports_json_object=True,
    supports_json_schema=False,
    send_temperature=False,
    never_send=("thinking_budget", "enable_thinking", "temperature", "top_p", "top_k"),
    requires_json_keyword=True,
    notes=(),
)

# GLM-5.3 on the Zhipu endpoint.
GLM_53_ZHIPU = ModelProfile(
    model="glm-5.3",
    vendor="zhipu",
    provider=ZHIPU,
    effort_enum=("low", "high", "max"),
    effort_default="max",
    max_out_by_effort={"low": 12_000, "high": 32_000, "max": 48_000},
    max_out_param="max_tokens",
    supports_json_object=True,
    supports_json_schema=False,
    send_temperature=False,
    never_send=("thinking", "top_p", "top_k", "temperature"),
    requires_json_keyword=False,
    notes=(),
)

# GLM-5.3 on a third-party host.
GLM_53_DASHSCOPE = ModelProfile(
    model="glm-5.3",
    vendor="zhipu",
    provider=DASHSCOPE,
    effort_enum=("low", "high", "max"),
    effort_default="max",
    max_out_by_effort={"low": 12_000, "high": 32_000, "max": 48_000},
    max_out_param="max_tokens",
    supports_json_object=True,
    supports_json_schema=False,
    send_temperature=False,
    never_send=("thinking", "top_p", "top_k", "temperature"),
    requires_json_keyword=False,
    notes=(),
)

# DeepSeek-V4.1-Flash（用户指定：DeepSeek 只用这一个模型）
# 官方只暴露滚动别名 deepseek-flash 与 deepseek-v4-pro；多源核实
# deepseek-flash 即 V4.1-Flash（旧名 deepseek-v4-flash 路由到它）。
#
# Note: reasoning_effort here is a string enum, not a 1-100 integer.
#   实际 API 拒收整数与数字字符串，报
#     unknown variant `30`, expected one of
#     `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`
DEEPSEEK_V41_FLASH = ModelProfile(
    model="deepseek-flash",
    vendor="deepseek",
    provider=DEEPSEEK,
    effort_enum=("none", "minimal", "low", "medium", "high", "xhigh", "max"),
    effort_default="medium",
    max_out_by_effort={"none": 8_000, "minimal": 10_000, "low": 16_000,
                       "medium": 28_000, "high": 40_000, "xhigh": 56_000,
                       "max": 64_000},
    max_out_param="max_tokens",
    supports_json_object=True,
    supports_json_schema=False,
    send_temperature=False,
    never_send=("top_p", "top_k", "temperature", "thinking"),
    requires_json_keyword=True,
    notes=(),
)

for _p in (GEMINI_37, GEMINI_38, GEMINI_37_FREE, GEMINI_38_FREE, GLM_53,
           QWEN_38_MAX, QWEN_38_MAX_DASHSCOPE, QWEN_38_MAX_DASHSCOPE_ROLLING,
           GLM_53_ZHIPU, GLM_53_DASHSCOPE, DEEPSEEK_V41_FLASH):
    PROFILES[_p.key] = _p

def get(model: str) -> ModelProfile:
    p = PROFILES.get(model)
    if p is None:
        raise KeyError(
            f"未知模型 {model!r}。已登记: {sorted(PROFILES)}。"
            "add a profile in this file before using a new model."
        )
    return p
