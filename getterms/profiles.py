"""每个模型「发什么参数、不发什么参数」的剖面。

所有事实来自 aihubmix 的机读权威 schema（2026-09-16 实拉核验）：
  https://aihubmix.com/model-data/models/{id}.{hash}.json
取 hash 的顺序（实测过的坑，勿改）：
  1. 模型页 https://aihubmix.com/model/<id>/llms.txt 底部「Full parameter schema」
  2. 回退 https://aihubmix.com/model-data/index.json 的 path 字段
  ⚠ 实测 index.json 反而更旧：它给的 glm-5.3.f6f948fa / gemini-3.7-flash.6d99cd38 /
    qwen3.8-max.7d0f9085 全部 NoSuchKey，而 llms.txt 给的 241ab058 / dba9de29 /
    b4e21211 可拉到。**以 llms.txt 为准。**

本轮实拉核验通过的 hash：
  gemini-3.7-flash.dba9de29   gemini-3.8-flash.bed7e4ca
  glm-5.3.241ab058            qwen3.8-max.b4e21211
（qwen3.8-max-2026-09-02 的 schema 路径已失效，但它是 qwen3.8-max 的日期快照，
  官方 reasoning_effort 说明为「Qwen3.8 系列」级，参数面同源。）
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Provider:
    """一个 API 供应商：端点 + key 文件 + 建议并发上限。

    2026-09-16 实测发现中转站不忠实转发国产模型的推理档位（详见计划 §5）：
      * Qwen：中转站三档全按默认 xhigh 跑，直连 medium 便宜 6.7 倍、快 5 倍；
      * GLM：中转站 high/max 正常，low 档坏掉（6257 思考 token，比自己的 max 还高）。
    所以 Qwen/GLM 走直连，Gemini 留中转站（那是 Google 自家兼容层，档位单调正常）。

    `max_concurrency` 是实测踩出来的：智谱直连在并发 12 时 26/44 批返回
    429（code 1302「您的账户已达到速率限制」），整轮 bake-off 数据作废重跑。
    超过这个值时 run.py / bakeoff.py 会告警并自动收敛。
    """
    name: str
    base_url: str
    key_file: str
    max_concurrency: int = 50
    rate_limit_note: str = ""


INFERERA = Provider("inferera", "https://api.inferera.com/v1", "aihubmix.txt",
                    max_concurrency=100,
                    rate_limit_note="aihubmix 明示不限并发；从 50 起步，10 分钟无 429 再翻倍")
BAILIAN = Provider("bailian",
                   "https://dashscope.aliyuncs.com/compatible-mode/v1",
                   "bailian.txt", max_concurrency=20,
                   rate_limit_note="按账户等级动态分配，未见公开数字；保守取 20")
# 智谱：官方 docs.bigmodel.cn/cn/api/rate-limit 明确限的是「同一时刻正在处理中的
# 请求数量」，按账户权益等级分级（V0 免费=「并发极低」/ V1 积分≥2000 / V2 ≥10000
# / V3 ≥50000，积分与消费 1:1），且不同模型各有独立上限。提额需控制台申请、
# 10 个工作日审核。错误码 1302 = 触发用户速率限制。
# 我们这把 key 的表现与 V0 一致：high 档并发 4 勉强可过（每次 10~20s），
# max 档并发 1 都 429（每次 100s+，一直占着并发位）。故取 2，且 max 档基本不可用。
# 法语要用 GLM 时优先走百炼的第三方 glm-5.3，或走中转站（贵但不限并发）。
ZHIPU = Provider("zhipu", "https://open.bigmodel.cn/api/paas/v4", "glm.txt",
                 max_concurrency=2,
                 rate_limit_note="V0/低档账户；max 档基本不可用，改走百炼或中转站")
DEEPSEEK = Provider("deepseek", "https://api.deepseek.com/v1", "deepseek.txt",
                    max_concurrency=100,
                    rate_limit_note="官方文档载并发上限 2500，不是瓶颈")


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
    provider: Provider = INFERERA
    # DeepSeek 的 reasoning_effort 是 1~100 连续值，不是枚举
    effort_is_numeric: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def key(self) -> str:
        """注册名：中转站用裸模型名，直连加 provider 前缀。"""
        if self.provider is INFERERA:
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
# 权威事实（gemini-3.7-flash.dba9de29 / gemini-3.8-flash.bed7e4ca 实拉）：
#   reasoning_effort: enum [low, medium, high]，default "medium"
#   思考上限 low=1024 / medium=8192 / high=24576
#   temperature/top_p/top_k：官方明文「Remove temperature, top_p, and top_k」
#   response_format: json_object 可用；json_schema 可用但不支持完全递归 schema
#   context 1,048,576   max_output_tokens 65,536   thinking=hybrid
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
    notes=(
        "思考上限 low=1024 / medium=8192 / high=24576 token",
        "reasoning_effort 与 thinking_config / thinking_level / thinking_budget 互斥，只走 reasoning_effort",
        "缓存读 $0.075/M = 原价 1/10，系统提示做固定前缀可省钱",
    ),
    **_GEMINI_COMMON,
)

GEMINI_38 = ModelProfile(
    model="gemini-3.8-flash",
    notes=(
        "与 3.7 同价同上下文，但思考 token 约为 3.7 的两倍（AA 实测）",
        "thinking_level=minimal 在 3.8 上报 API validation error，不发",
    ),
    **_GEMINI_COMMON,
)

# 免费变体：只用于验证管线，价格/限流/行为以付费口为准
GEMINI_37_FREE = ModelProfile(model="gemini-3.7-flash-free",
                              notes=("免费变体，仅冒烟验证管线",), **_GEMINI_COMMON)
GEMINI_38_FREE = ModelProfile(model="gemini-3.8-flash-free",
                              notes=("免费变体，仅冒烟验证管线",), **_GEMINI_COMMON)

# ------------------------------------------------------------------- GLM-5.3
# 权威事实（glm-5.3.241ab058 实拉）：
#   reasoning_effort: enum [low, high, max]，default "max"
#     官方：「low: light; high: enhanced; max: deep」
#   thinking: always-on —— GLM-5.3 无法关闭思考（5.2 可以）
#     优先级 Explicit Effort > thinking toggle > 默认 max
#   response_format: enum [text, json_object] —— 没有 json_schema
#   temperature default 1.0（「多数推理模型不支持调节」）
#   context 1,048,576   max_output_tokens 131,072   开放权重 zai-org/GLM-5.3
#   aihubmix 明示不限并发
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
    notes=(
        "思考无法关闭；只发 reasoning_effort，不发 thinking",
        "默认档就是 max（最贵），必须显式降档",
        "reasoning_content 是思考内容，绝不能当结果解析",
        "aihubmix 明示不限并发",
    ),
)

# --------------------------------------------------------------- Qwen3.8-Max
# 权威事实（qwen3.8-max.b4e21211 实拉，逐字引官方）：
#   reasoning_effort: enum [low, medium, xhigh]，default "xhigh"
#     「xhigh（默认）：高力度推理；medium：中力度推理；low：低力度推理。
#       max 映射为 xhigh，high 映射为 xhigh，minimal 映射为 low，
#       none 映射为 enable_thinking=False。设置上述可选值及映射值以外的值将会报错。」
#     ⚠ 所以 high 等于没降档 —— bake-off 只跑 low / medium
#   thinking_budget: 与 reasoning_effort 互斥，同时设会报错 -> 不发
#   response_format: 只有 json_object（结构化输出页全文未提 json_schema）
#     且「System Message 或 User Message 中必须包含「JSON」关键词（不区分大小写）」
#     否则报 'messages' must contain the word 'json' in some form...
#   max_completion_tokens: 官方「including visible output tokens and reasoning tokens」
#     上限 131,072
#   context 1,000,000   thinking=hybrid
#   隐式缓存命中按输入价 20% 计，最少 256 token 触发；usage.prompt_tokens_details.cached_tokens
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
    notes=(
        "默认 xhigh 会烧掉大量思考 token，必须显式设档",
        "high/max 都被映射成 xhigh，不算降档，别用",
        "提示词里必须出现 JSON 字样（我们的提示词含多处 JSON，满足）",
        "reasoning_content 是思考内容，不能当结果解析",
        "日期固定版，跑批期间不会被静默换版",
    ),
)


PROFILES: dict[str, ModelProfile] = {}


# ============================================================ 官方直连剖面
# 为什么要有这一组：见本文件 Provider 的注释与计划 §5 的实测表。

# 百炼直连的 Qwen。⚠ 命名差异：中转站叫 qwen3.8-max-2026-09-02，
# 百炼叫 qwen3.8-max-0902（实拉百炼 252 个模型确认）。
QWEN_38_MAX_BAILIAN = ModelProfile(
    model="qwen3.8-max-0902",
    vendor="alibaba",
    provider=BAILIAN,
    effort_enum=("low", "medium", "xhigh"),
    effort_default="xhigh",
    max_out_by_effort={"low": 16_000, "medium": 28_000, "xhigh": 64_000},
    max_out_param="max_completion_tokens",
    supports_json_object=True,
    supports_json_schema=False,
    send_temperature=False,
    never_send=("thinking_budget", "enable_thinking", "temperature", "top_p", "top_k"),
    requires_json_keyword=True,
    notes=(
        "直连实测档位真的生效且单调：low 660 / medium 726 / xhigh 4808 思考 token",
        "比中转站便宜 6.7 倍、快 5 倍（中转站一律按 xhigh 跑）",
        "百炼上同时有第三方 glm-5.3 / deepseek-v4.1-flash，一个 key 可多用",
    ),
)

QWEN_38_MAX_BAILIAN_ROLLING = ModelProfile(
    model="qwen3.8-max",
    vendor="alibaba",
    provider=BAILIAN,
    effort_enum=("low", "medium", "xhigh"),
    effort_default="xhigh",
    max_out_by_effort={"low": 16_000, "medium": 28_000, "xhigh": 64_000},
    max_out_param="max_completion_tokens",
    supports_json_object=True,
    supports_json_schema=False,
    send_temperature=False,
    never_send=("thinking_budget", "enable_thinking", "temperature", "top_p", "top_k"),
    requires_json_keyword=True,
    notes=("滚动别名，跑批期间可能被静默换版；正式跑用 qwen3.8-max-0902",),
)

# 智谱直连的 GLM-5.3
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
    notes=(
        "直连实测完全单调：low 0 / high 121 / max 659 思考 token",
        "中转站的 low 档坏掉（6257），high/max 两档中转站可用",
        "⚠ 智谱账户档位限制严重（见 ZHIPU 注释）：并发只能给 2，max 档基本跑不动。"
        "法语要用 GLM 优先走 bailian/glm-5.3",
    ),
)

# 百炼上的第三方 GLM-5.3 —— 法语轮次的首选路线。
# 为什么要有这一条：智谱直连的账户档位把并发压到 2 且 max 档不可用；
# 中转站虽不限并发但把 GLM 的思考 token 放大约 5 倍（外推 $267 vs 直连 $48）。
# 百炼是付费的阿里云账户、并发额度高得多，且不经中转站转发，档位应当忠实。
# 实拉百炼 252 个模型确认 glm-5.3 在架（另有 glm-5.2 / glm-5.1 / glm-5）。
GLM_53_BAILIAN = ModelProfile(
    model="glm-5.3",
    vendor="zhipu",
    provider=BAILIAN,
    effort_enum=("low", "high", "max"),
    effort_default="max",
    max_out_by_effort={"low": 12_000, "high": 32_000, "max": 48_000},
    max_out_param="max_tokens",
    supports_json_object=True,
    supports_json_schema=False,
    send_temperature=False,
    never_send=("thinking", "top_p", "top_k", "temperature"),
    requires_json_keyword=False,
    notes=(
        "第三方托管：模型是智谱的，账户与限流是阿里云的",
        "阿里云百炼 GLM 文档明示：Anthropic 兼容模式不支持 reasoning_effort，"
        "要用原生 thinking 参数；我们走 OpenAI 兼容口，用 reasoning_effort",
    ),
)

# DeepSeek-V4.1-Flash（用户指定：DeepSeek 只用这一个模型）
# 官方只暴露滚动别名 deepseek-flash 与 deepseek-v4-pro；多源核实
# deepseek-flash 即 V4.1-Flash（旧名 deepseek-v4-flash 路由到它）。
#
# ⚠ 网上说法被实测推翻：某些技术博客写「reasoning_effort 是 1~100 连续值」，
#   实际 API 拒收整数与数字字符串，报
#     unknown variant `30`, expected one of
#     `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`
#   —— 是完整 7 档字符串枚举。这就是为什么参数一律以实拉/实测为准，不采信博客。
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
    notes=(
        "价格只有 Gemini 的 1/3：峰时 $0.30/$1.20，谷时 $0.15/$0.60（缓存 $0.003）",
        "上下文 1M、最大输出 384K、并发上限 2500、MIT 开放权重",
        "档位枚举实测为 7 档 none~max（不是博客说的 1~100 连续值）",
        "有 none 档可完全关思考，配上 1/3 的价格可能是最便宜的选择",
        "竞技场西语榜没有它，无质量证据 —— 只能当便宜黑马试，不能凭价格选",
    ),
)


for _p in (GEMINI_37, GEMINI_38, GEMINI_37_FREE, GEMINI_38_FREE, GLM_53,
           QWEN_38_MAX, QWEN_38_MAX_BAILIAN, QWEN_38_MAX_BAILIAN_ROLLING,
           GLM_53_ZHIPU, GLM_53_BAILIAN, DEEPSEEK_V41_FLASH):
    PROFILES[_p.key] = _p

# bake-off 第一步的参赛表（计划 §4.4，已按 2026-09-16 冒烟实测调整）
#
# 实测（同一批 5 句 conf/poli，v1 提示词，见 out/smoke_matrix.json 与 cache/）：
#   gemini-3.7-flash  low/medium 思考=0，high 思考=970    延迟 7~20s
#   gemini-3.8-flash  low/medium 思考=0，high 思考=1309   延迟 15~47s（波动大）
#   glm-5.3           low 思考=6257(!) 68s，high 393 9.9s，max 708 11.9s
#                     —— low 档反而最慢最贵，且只有 GLM 出现缓存命中(512/670)
#   qwen3.8-max       low 4455/128.8s，medium 4843/138.6s，xhigh 4182/118.8s
#                     —— 三档几乎无差别，档位形同无效；外推全量约 $256，是计划估值两倍
#
# 据此调整：
#   * Qwen 由两档缩为一档（low 与 medium 实测无区别，跑两次是浪费）
#   * GLM 补上 max 档（high vs max 都便宜，值得比质量）
#   * GLM 的 low 档排除（实测最慢最贵，没有理由跑）
BAKEOFF_ENTRIES = [
    ("gemini-3.7-flash", "medium"),          # 实测 0 思考，最便宜的主候选
    ("gemini-3.7-flash", "high"),            # 对标榜单的 -high 条目
    ("gemini-3.8-flash", "high"),            # 只回答一个问题：贵一倍的思考换来多少
    ("glm-5.3", "high"),                     # 便宜 + 唯一有缓存命中
    ("glm-5.3", "max"),                      # 榜单法语 #4 就是这一档
    ("qwen3.8-max-2026-09-02", "medium"),    # 西语 #7，但成本待确认
]


def get(model: str) -> ModelProfile:
    p = PROFILES.get(model)
    if p is None:
        raise KeyError(
            f"未知模型 {model!r}。已登记: {sorted(PROFILES)}。"
            "新增模型请先按本文件头部的顺序拉 model-data JSON，照它写剖面，不要凭记忆。"
        )
    return p
