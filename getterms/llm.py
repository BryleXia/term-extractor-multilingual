"""LLM 适配层：一个薄的 OpenAI 兼容客户端 + 重试 + usage 采集 + 响应缓存。

设计要点：
  * 单次超时 300s（Qwen 官方：非流式超过 300 秒未完成会被上游中断）；
  * 429/5xx/超时 -> 指数退避重试，确定性上限；
  * usage 里的 reasoning_tokens / cached_tokens 全量采集（预算外推靠它）；
  * 响应按 (model, effort, prompt_hash, file_md5, batch_idx) 落盘缓存 ——
    断点续跑不重花钱；改提示词会换 prompt_hash，等于该模型全量重跑（计划 §6）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import threading
import time
from dataclasses import dataclass, field, asdict, fields
from pathlib import Path

from openai import AsyncOpenAI

from .config import CACHE_DIR, cost_usd, load_api_key
from .profiles import ModelProfile, get as get_profile

SINGLE_CALL_TIMEOUT = 300.0
MAX_ATTEMPTS = 5
RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
# 单批所有尝试合起来的时长上限。为什么需要：`_sem` 的槽在整个重试循环内**不放**，
# 所以最坏情况一个槽被占 5 x 300s + 约 30s 退避 = 约 25 分钟，而墙钟预估常量
# 42s/批完全不含这种情况。50 并发下几十批同时卡超时会把 1.4 小时拖成几小时。
# 超了就不再重试（那一批记失败、进不完整闸门、且不落缓存 -> 重跑会重试）。
BATCH_TIME_BUDGET = 900.0

# 「账户没钱了」的消息特征，**大小写不敏感的子串匹配**。
# 为什么必须匹配文本而不是看状态码：403 不只代表余额不足 —— key 失效、模型无权限、
# 地区限制都是 403。中转站（inferera / aihubmix）的实际说法是
#     API Error: 403 Your account balance is insufficient.
# 但别家措辞不同（OpenAI 官方用 exceeded your current quota），所以留一组。
BALANCE_PATTERNS = (
    "insufficient balance",
    "insufficient account balance",
    "insufficient credit",
    "insufficient quota",
    "insufficient funds",
    "balance is insufficient",
    "exceeded your current quota",
    "余额不足",
    "额度不足",
    "欠费",
)


def is_balance_exhausted(status: int | None, text: str | None) -> bool:
    """这次失败是不是「账户没钱了」。

    **终局判断**：一旦为真，后面每一次调用都会同样失败，没有重试的意义。
    所以 run.py 的熔断阈值是「第一次命中就停」，不是 429 那种「连续 20 次」。

    为什么是纯函数：埋在 `call()` 的 except 里的判据，外部只能读源码字符串断言，
    而那证明不了行为 —— 429 熔断第一版就是「源码断言全过、实测无效」。

    402 Payment Required 按定义就是付款问题，直接判真，不看消息。
    """
    if status == 402:
        return True
    # ⚠ 匹配前把分隔符归一化：同一个意思三种写法都出现过 ——
    #   `insufficient balance`（中转站）、`insufficient_quota`（OpenAI 的
    #   error code 用下划线）、`insufficient-balance`（有的网关用连字符）。
    #   逐个列举变体会把模式表撑成一堆近似重复的串，归一化一行就够。
    #   在错误文本上做这个没有风险：只在失败路径跑，且模式本身已足够具体。
    low = (text or "").lower().replace("_", " ").replace("-", " ")
    return any(p in low for p in BALANCE_PATTERNS)


@dataclass
class CallResult:
    ok: bool
    content: str = ""
    finish_reason: str | None = None
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    latency_s: float = 0.0
    attempts: int = 0
    error: str | None = None
    from_cache: bool = False
    model: str = ""
    effort: str | None = None
    # 这次请求真正发出去的 wire 参数（剖面 build_kwargs + 覆盖）的指纹。
    # ⚠ 缓存键里**没有**它（键 = 注册名+effort+phash+file_md5+批内容哈希），
    #   所以改 profiles.py 的 max_out_by_effort / temperature 之类不会换键，
    #   旧缓存继续命中、新参数形同没改。写进内容里，读的时候比对。
    #   旧缓存没这个字段 -> 默认空串 -> 照旧接受（不制造一次全量重跑）。
    wire_fp: str = ""

    @property
    def cost(self) -> float:
        return cost_usd(self.model, self.prompt_tokens, self.cached_tokens,
                        self.completion_tokens)

    @property
    def refused(self) -> bool:
        """空响应或被安全过滤 —— 第三档语料（conf/poli）的关键风险信号。"""
        if self.finish_reason in ("content_filter", "refusal"):
            return True
        return self.ok and not self.content.strip()

    @property
    def truncated(self) -> bool:
        return self.finish_reason == "length"


def prompt_hash(system: str, user_template: str) -> str:
    h = hashlib.sha256()
    h.update(system.encode("utf-8"))
    h.update(b"\x00")
    h.update(user_template.encode("utf-8"))
    return h.hexdigest()[:12]


class LLMClient:
    def __init__(self, model: str, effort: str | None, phash: str,
                 concurrency: int = 8, use_cache: bool = True,
                 param_override: dict | None = None):
        self.profile: ModelProfile = get_profile(model)
        if not self.profile.effort_ok(effort):
            allowed = ("1~100 的整数" if self.profile.effort_is_numeric
                       else str(self.profile.effort_enum))
            raise ValueError(
                f"{model} 的 reasoning_effort 只接受 {allowed}，收到 {effort!r}"
            )
        self.model = model
        self.effort = effort
        self.phash = phash
        self.use_cache = use_cache
        # 只给冒烟测试用：覆盖剖面产出的参数（如故意把输出上限压到 2000）
        self.param_override = dict(param_override or {})
        prov = self.profile.provider
        # 按供应商的实测上限收敛并发，避免整轮被 429 打废（智谱踩过）
        self.concurrency = min(concurrency, prov.max_concurrency)
        if self.concurrency < concurrency:
            print(f"  [并发收敛] {prov.name} 建议上限 {prov.max_concurrency}，"
                  f"已把 {concurrency} 降为 {self.concurrency}"
                  + (f"（{prov.rate_limit_note}）" if prov.rate_limit_note else ""))
        self._sem = asyncio.Semaphore(self.concurrency)
        self._client = AsyncOpenAI(
            api_key=load_api_key(prov.key_file),
            base_url=prov.base_url,
            timeout=SINGLE_CALL_TIMEOUT,
            max_retries=0,          # 重试我们自己做，便于统计
        )
        # 缓存目录用注册名（含 provider 前缀），避免直连与中转站的结果互相污染
        tag = f"{self.profile.key.replace('/', '__')}__{effort or 'default'}"
        self.cache_root = CACHE_DIR / tag / phash
        self.stats: dict[str, int] = {}
        self._wfp: str | None = None

    @property
    def price_key(self) -> str:
        """价目表的键 = 剖面注册名。"""
        return self.profile.key

    # ------------------------------------------------------------- 缓存

    def _cache_path(self, file_md5: str, batch_idx: int,
                    batch_key: str = "") -> Path:
        """缓存文件名必须能唯一确定「这次请求的输入」。

        ⚠ 2026-09-16 修的 bug：原先只用 (file_md5, batch_idx)，**不含批内容**。
        换批大小时同一个 batch_idx 对应的句子集合会变（批 10 的第 0 批是 1~10 句，
        批 20 的第 0 批是 1~20 句），键却相同 —> 命中为另一种分批算出的旧响应，
        随后校验会把 sent_id 不在本批的术语全丢掉，**静默损失召回**。
        现在把批内容的哈希放进文件名。
        """
        suffix = f"__{batch_key}" if batch_key else ""
        return (self.cache_root / file_md5[:2]
                / f"{file_md5}__b{batch_idx:04d}{suffix}.json")

    @property
    def wire_fp(self) -> str:
        """wire 参数的指纹。惰性算一次 —— build_kwargs 在一轮里是常量。"""
        if self._wfp is None:
            try:
                blob = json.dumps(self.wire_params(), sort_keys=True,
                                  ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                blob = repr(sorted(self.wire_params().items()))
            self._wfp = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]
        return self._wfp

    def invalidate(self, file_md5: str, batch_idx: int,
                   batch_key: str = "") -> bool:
        """把一条缓存撤掉，让重跑真的会重新调用。

        ⚠ 只给「响应拿到了、但解析不出 JSON 数组」用（`extract.run_batch`）。
          那种响应 refused / truncated 都是假，会正常落盘，而重跑时首轮与纠正
          重试**双双命中缓存**、结果逐字节相同 —— 一分钱不花，也一点进展没有。
          闸门提示里「重跑同一条命令补齐失败批」这条唯一的补救路径，
          对这一类批在撤缓存之前是**永久无效**的。

        不撤「模型给了合法空数组」的缓存 —— 那是最终答案，见 run_batch 的注释。
        """
        p = self._cache_path(file_md5, batch_idx, batch_key)
        try:
            p.unlink()
        except FileNotFoundError:
            return False
        except OSError:
            self.stats["cache_unlink_fail"] = (
                self.stats.get("cache_unlink_fail", 0) + 1)
            return False
        self.stats["cache_invalidated"] = (
            self.stats.get("cache_invalidated", 0) + 1)
        return True

    def _read_cache(self, file_md5: str, batch_idx: int,
                    batch_key: str = "") -> CallResult | None:
        if not self.use_cache:
            return None
        p = self._cache_path(file_md5, batch_idx, batch_key)
        if not p.exists():
            return None
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            d.pop("from_cache", None)
            # ⚠ 未知键必须丢掉、构造必须在 try 里。
            #   原来 `CallResult(**d)` 在 try 之外：只要这个 dataclass 增删一个字段
            #   （本轮就要加），旧缓存会抛 TypeError，而 TypeError 不是「未命中」——
            #   它冒到 run.py worker 的 except Exception，被记成本地异常 + 失败批，
            #   于是**已缓存的批集体变成失败批**，18,600 批全红、闸门拦住交付包。
            #   缓存读不出来的唯一正确语义是「未命中 → 重调一次」。
            known = {f.name for f in fields(CallResult)}
            r = CallResult(**{k: v for k, v in d.items() if k in known})
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            return None
        # wire 参数变了就不能复用 —— 否则「我改了参数」与「结果没变」同时成立，
        # 而你看不出是哪一个。旧缓存没有指纹（空串）时照旧接受，只计数。
        if r.wire_fp and r.wire_fp != self.wire_fp:
            self.stats["cache_param_mismatch"] = (
                self.stats.get("cache_param_mismatch", 0) + 1)
            return None
        if not r.wire_fp:
            self.stats["cache_no_fp"] = self.stats.get("cache_no_fp", 0) + 1
        r.from_cache = True
        return r

    def _write_cache(self, file_md5: str, batch_idx: int, r: CallResult,
                     batch_key: str = "") -> None:
        """原子落盘：先写临时文件再 os.replace。

        为什么要原子：高并发（50）下按 Ctrl-C，可能有几十个响应正写到一半，
        留下截断的 JSON。`_read_cache` 会捕获 JSONDecodeError 当作未命中，
        所以不会读到脏数据，但那几批要重新花钱调一次。os.replace 在同一卷上
        是原子的（Windows 也是），杜绝这种半截文件。
        """
        if not self.use_cache:
            return
        p = self._cache_path(file_md5, batch_idx, batch_key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + f".tmp{os.getpid()}_{threading.get_ident()}")
        try:
            tmp.write_text(json.dumps(asdict(r), ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, p)
        except OSError:
            tmp.unlink(missing_ok=True)     # 落盘失败不该拖垮整轮，下次重调即可
            # ⚠ 但必须有数：原先这里一声不响，于是 cache/ 不可写 / 盘满 / 被杀软锁住时
            #   18,600 批一个都没缓存，而你完全看不出来 —— 随后任何一次中断都是
            #   全价重跑。run.py 收尾会打印 client.stats，这个键就露出来了。
            self.stats["cache_write_fail"] = self.stats.get("cache_write_fail", 0) + 1

    # ------------------------------------------------------------- 调用

    @staticmethod
    def _usage(resp) -> tuple[int, int, int, int]:
        u = getattr(resp, "usage", None)
        if u is None:
            return 0, 0, 0, 0
        pt = getattr(u, "prompt_tokens", 0) or 0
        ct = getattr(u, "completion_tokens", 0) or 0
        pd = getattr(u, "prompt_tokens_details", None)
        cached = (getattr(pd, "cached_tokens", 0) or 0) if pd else 0
        cd = getattr(u, "completion_tokens_details", None)
        reasoning = (getattr(cd, "reasoning_tokens", 0) or 0) if cd else 0
        return pt, cached, ct, reasoning

    def wire_params(self) -> dict:
        """本客户端真正会发出去的参数（剖面 + 覆盖）。"""
        kw = self.profile.build_kwargs(self.effort)
        kw.update(self.param_override)
        return kw

    async def call(self, messages: list[dict], file_md5: str,
                   batch_idx: int, batch_key: str = "") -> CallResult:
        cached = self._read_cache(file_md5, batch_idx, batch_key)
        if cached is not None:
            self.stats["cache_hit"] = self.stats.get("cache_hit", 0) + 1
            return cached

        kwargs = self.wire_params()
        last_err = None
        async with self._sem:
            t_batch0 = time.monotonic()
            for attempt in range(1, MAX_ATTEMPTS + 1):
                t0 = time.monotonic()
                try:
                    resp = await self._client.chat.completions.create(
                        model=self.profile.model, messages=messages, **kwargs
                    )
                    dt = time.monotonic() - t0
                    ch = resp.choices[0] if resp.choices else None
                    content = ""
                    finish = None
                    if ch is not None:
                        finish = getattr(ch, "finish_reason", None)
                        msg = getattr(ch, "message", None)
                        content = (getattr(msg, "content", None) or "") if msg else ""
                        # 思考内容绝不当结果解析（Qwen/GLM 的 reasoning_content）
                    pt, cc, ct, rt = self._usage(resp)
                    r = CallResult(
                        ok=True, content=content, finish_reason=finish,
                        prompt_tokens=pt, cached_tokens=cc,
                        completion_tokens=ct, reasoning_tokens=rt,
                        latency_s=round(dt, 3), attempts=attempt,
                        model=self.price_key, effort=self.effort,
                        wire_fp=self.wire_fp,
                    )
                    # ⚠ refused（空响应 / finish_reason 为 content_filter/refusal）
                    #   与 truncated（finish_reason == "length"）**都不写缓存**。
                    #   以前先写后判，被安全过滤的批会永久留在缓存里，重跑同一条命令
                    #   不会再重试它，那些句子在交付物里表现为「没有术语」，与真的没术语
                    #   无法区分。时政层（conf/poli）尤其相关。2026-09-18 加固。
                    # ⚠ 2026-09-19 补：原先只挡 refused，**漏了 truncated**。
                    #   截断响应 `refused` 为假（ok=True 且 content 非空），照样落缓存
                    #   —— 同一个永久无声丢批的坑，只是入口不同。142 批的样本里
                    #   truncated=0，所以一直没暴露；全量 18,600 批必然撞上。
                    if not r.refused and not r.truncated:
                        self._write_cache(file_md5, batch_idx, r, batch_key)
                    self.stats["calls"] = self.stats.get("calls", 0) + 1
                    if r.refused:
                        self.stats["refused"] = self.stats.get("refused", 0) + 1
                    if r.truncated:
                        self.stats["truncated"] = self.stats.get("truncated", 0) + 1
                    return r
                except Exception as e:  # noqa: BLE001 —— 网关错误形态多样，统一处理
                    last_err = e
                    status = getattr(e, "status_code", None)
                    retriable = (
                        status in RETRY_STATUS
                        or status is None            # 超时/连接错误
                        or isinstance(e, (asyncio.TimeoutError, TimeoutError))
                    )
                    self.stats[f"err_{status or 'net'}"] = (
                        self.stats.get(f"err_{status or 'net'}", 0) + 1
                    )
                    if status == 429:
                        self.stats["rate_limited"] = (
                            self.stats.get("rate_limited", 0) + 1)
                    # 「账户没钱了」单独计数。它**不是**瞬时故障，重试没有意义，
                    # 所以 run.py 一见它就把新批全停掉（第一次命中即停）。
                    # 这一批本身不落缓存（失败路径），充值后重跑会精确重试它。
                    if is_balance_exhausted(status, str(e)):
                        self.stats["err_balance"] = (
                            self.stats.get("err_balance", 0) + 1)
                    if not retriable or attempt == MAX_ATTEMPTS:
                        break
                    # ⚠ 占槽预算：`_sem` 在整个重试循环内不放，最坏 25 分钟。
                    #   超了就别再重试了 —— 这一批记失败、不落缓存、重跑会重试，
                    #   比占着 50 个槽之一把墙钟拖垮划算。
                    if time.monotonic() - t_batch0 > BATCH_TIME_BUDGET:
                        self.stats["time_budget_break"] = (
                            self.stats.get("time_budget_break", 0) + 1)
                        break
                    # 网关给了 Retry-After 就听它的（封顶 60s），别自己瞎退避。
                    ra = None
                    _rsp = getattr(e, "response", None)
                    if _rsp is not None:
                        try:
                            ra = float((getattr(_rsp, "headers", None)
                                        or {}).get("retry-after") or 0) or None
                        except (TypeError, ValueError, AttributeError):
                            ra = None
                    backoff = min(60.0, ra or 2.0 ** attempt) + random.uniform(0, 1.5)
                    await asyncio.sleep(backoff)

        return CallResult(
            ok=False, attempts=MAX_ATTEMPTS,
            error=f"{type(last_err).__name__}: {last_err}"[:500],
            model=self.price_key, effort=self.effort,
        )

    async def aclose(self) -> None:
        await self._client.close()
