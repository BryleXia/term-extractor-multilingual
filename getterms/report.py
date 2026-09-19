"""运行报告：调用数、token、成本、校验失败率、拒答率、§2 异常处置。

报告是给人看的（用户 + 语言老师 + 王敬），也是 bake-off 的数据来源。
"""
from __future__ import annotations

import collections
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import cost_usd


# 各语种全量可用句对（getterms.audit 实跑）。成本外推的分母，改语料要同步改这里。
FULL_CORPUS = {
    "es": ("西语", 56110),      # md5 去重后实际要调的句对
    "fr": ("法语", 65097),      # 法语无 md5 重复组，等于可用句对
    # 俄语全量 2026-09-18 到手（9 个 zip / 533 文件 / 58,416 句），audit 实测可用 57,469。
    # 原来那 10 个测试文件挪到 config.CORPUS_DIRS["ru_test"]。
    "ru": ("俄语", 57469),
}


@dataclass
class RunReport:
    model: str
    effort: str | None
    prompt: str
    phash: str
    batch_size: int
    concurrency: int
    lang: str = "es"          # 决定成本外推用哪个语种的全量句数（FULL_CORPUS）
    started: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    finished: str = ""

    # 调用层
    calls: int = 0
    cache_hits: int = 0
    call_failures: int = 0
    refused: int = 0
    truncated: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    latency_sum: float = 0.0
    # ⚠ 缓存命中的批也会累加上面那些 token（这是有意的：报告要能说明整轮的规模）。
    #   所以 `cost` 是「整轮累计成本」，**续跑时不等于本轮真实花掉的钱**。
    #   报给学院的数字要用 new_cost_usd —— 只累加 from_cache=False 的调用。
    new_cost_usd: float = 0.0
    # gather 之外的本地异常（形态分析器、校验、类型归一化路径上的意外）。
    # 以前这类异常会炸掉整个 gather，一份交付物都不产出。
    batch_exceptions: int = 0
    # 交付前把「同文件同中文术语同切片却给了多个词典形」统一成多数票的处数。
    # 不是错误计数 —— 是模型抖动被装配层收口的次数（实测量级：1,854 条里 1 处）。
    dict_form_unified: int = 0
    # 形态分析器是否真的启用。None = 该语种不走词典形。
    morph_enabled: bool | None = None

    # 解析与校验层
    batches: int = 0
    json_failed_batches: int = 0
    # ---- 「运行不完整」的三类，全部要进闸门、而且要能定位到批
    # ⚠ 以前只有 call_failures 进闸门；refused 与 JSON 死批的 ok=True，
    #   既不算失败也不进清单，那些句子在 xlsx 里表现为「没有术语」，
    #   与真的没术语无法区分，报告里也查不到是哪几批。
    refused_batches: int = 0        # 最后一次调用仍是拒答/空响应的批
    json_dead_batches: int = 0      # 纠正重试之后仍解析不出数组的批
    bad_shape: int = 0              # 畸形结构条目数（不是批数）
    no_terms_key: int = 0           # 只是没给 terms 键，**不算错**
    dead_batches: list[str] = field(default_factory=list)   # 可定位清单，封顶 500
    skipped_by_budget: int = 0      # 熔断后没有派发的批数（花费熔断或 429 熔断）
    # 停止派发的原因。空 = 没停过。写进 gate_verdict，因为「调高 --max-spend」
    # 这条建议对 429 熔断是**错的**（那要降并发）。
    stop_reason: str = ""
    # 熔断跳过的批的**可定位清单**。⚠ 以前只有计数 —— 跑完只知道「跳过 N 批」，
    # 不知道是哪 N 批，而这 N 批的句子在 xlsx 里和「真的没术语」一模一样。
    skipped_batches: list[str] = field(default_factory=list)
    # dead_batches 封顶 500；这个记**一共有过多少条**，用来在清单末尾说明
    # 「还有 N 条未列出」—— 以前超 500 是**静默截断**，清单看着是完整的。
    dead_batches_total: int = 0
    # json_dead 的两种成因，处置相反，必须分开看：
    #   parse_dead  = 真的解析不出来。我们**丢失了**这批的答案；缓存已被
    #                 run_batch 的 _finish() 撤掉，重跑会重新调用。
    #   empty_array = 模型给了合法的 `[]`。它**明确说**这批没术语，是最终答案；
    #                 缓存保留，否则每次重跑都为「确实没术语」重新付费。
    # 两者在交付物里表现一样（那几句没术语），所以进闸门同权。
    parse_dead_batches: int = 0
    empty_array_batches: int = 0
    # ⚠ 第三类「这批没有术语」：模型回了**逐句对象**、但每句的 `terms` 都是空数组。
    #   它走的是**正常解析路径**，上面两个计数器都不动 —— 2026-09-19 全量审计
    #   才发现西语有 96 个批（1.64% / 605 句）属于这一类，而报告是**完全看不见**的。
    #   大多是真的没术语；但也可能是模型在犯错，或**上游句对根本对不上**
    #   （实测最大的一个正是后者）。**看不见就无法判断**，所以先让它可见。
    #   ⚠ 它**不进** `incomplete_batches` 闸门 —— 「模型明确说没术语」与
    #   「我们不知道有没有」是两回事，混进去会让闸门误伤正常运行。
    empty_batches: int = 0
    sentences_empty: int = 0
    empty_batch_list: list[str] = field(default_factory=list)   # 封顶 500
    # **交付后**的「词典形列 != 句中切片列」行数（第 8/9 列 vs 第 4/5 列）。
    # ⚠ 与 `nom_changed` **不是同一个数**：后者是收口前（`normalize_delivery`
    #   与 `unify_dict_forms` 都在它之后才改交付值）。下游拿 `nom_changed` 核 xlsx
    #   会以为少了 111 行（西语实测 14,669 vs 14,780）。核交付请用这个。
    rows_dict_changed: int = 0
    # 429 次数（按**尝试**计，不是按批）。非 0 就值得看一眼并发是不是开大了。
    rate_limited: int = 0
    # 「账户余额不足」的次数。非 0 意味着这一轮**没跑完**，而且不是质量问题 ——
    # 失败的批不落缓存，充值后重跑同一条命令即可精确续传。
    balance_errors: int = 0
    # 交付值标点归一化的处数。**不是错误数** —— 是把外语侧的外层引号与撇号字符
    # 收口成一致写法（`Yan’an` / `Yan'an`、`proyecto "X"` -> `proyecto X`）。
    delivery_normalized: int = 0
    terms_proposed: int = 0
    terms_kept: int = 0
    bad_anchor_src: int = 0
    bad_anchor_tgt: int = 0
    bad_types: int = 0
    bad_sent_id: int = 0
    correction_retries: int = 0

    # 词典形还原（`extract.DICT_FORM_LANGS` 里的语种；其余恒为 0，不打印这一节）
    nom_ok: int = 0
    nom_fallback: int = 0
    nom_failures: list = field(default_factory=list)
    nom_changed: int = 0          # 交付值与句中形式不同 = 真的做了还原
    nom_plural_kept: int = 0      # 还原后中心词仍是复数（习惯复数，供人工抽看）
    nom_reject_same: int = 0      # 校验不过但词典形 = 切片 -> 交付值无损失，不算真实损失
    # 补语（介词后的名词）的数被改了。校验器抓不到这类错（实测 5/5 放行），只能人看。
    # 法语老师 2026-09-18 的口径（类别定语->单数、构成实体->复数）就落在这个指标上。
    nom_comp_changed: int = 0
    nom_comp_notes: list = field(default_factory=list)

    # 文件层
    files_total: int = 0
    files_called: int = 0        # md5 去重后实际调用的文件数
    files_written: int = 0
    files_zero_terms: int = 0
    sentences_total: int = 0
    sentences_sent: int = 0

    per_file: dict = field(default_factory=dict)
    per_layer: dict = field(default_factory=lambda: collections.defaultdict(
        lambda: {"files": 0, "sent": 0, "terms": 0, "refused": 0}))
    corpus_issues: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)

    # ------------------------------------------------------------ 派生指标

    @property
    def cost(self) -> float:
        return cost_usd(self.model, self.prompt_tokens, self.cached_tokens,
                        self.completion_tokens)

    @property
    def new_spend_usd(self) -> float:
        """本轮真实新增支出（不含缓存命中的批）。断点续跑后要看这个数，不是 cost。"""
        return self.new_cost_usd

    @property
    def batches_attempted(self) -> int:
        """扣掉「调用层就失败」的批次 —— 那不是模型输出质量问题。"""
        return max(0, self.batches - self.call_failures)

    @property
    def json_ok_rate(self) -> float:
        """JSON 解析成功率，**只在真的拿到响应的批次上算**。

        2026-09-16 修的 bug：原先拿 batches 做分母，于是被限流的运行
        （智谱直连 26/44 次 429）会被算成「JSON 成功率 40%」而按质量淘汰。
        限流是我们自己的并发配置问题，不是模型的问题，必须分开报。
        """
        denom = self.batches_attempted
        if denom <= 0:
            return 0.0
        parse_failed = max(0, self.json_failed_batches - self.call_failures)
        return 1.0 - parse_failed / denom

    @property
    def call_failure_rate(self) -> float:
        return self.call_failures / self.batches if self.batches else 0.0

    @property
    def incomplete_batches(self) -> int:
        """「这一批的句子没能产出术语，而我们不知道它本该有没有」的批数。

        闸门用这个，不是只用 call_failures。三类同权：调用层失败、拒答/空响应、
        纠正重试后仍解析不出数组。三类在交付物里的表现完全一样 ——
        那几句看起来「没有术语」。
        """
        # ⚠ 熔断跳过的批也算。实测漏过一次：skipped_by_budget=3 而交付包照样产出，
        #   那 3 批的句子在 xlsx 里同样表现为「没有术语」。
        return (self.call_failures + self.refused_batches
                + self.json_dead_batches + self.skipped_by_budget)

    @property
    def incomplete_rate(self) -> float:
        return self.incomplete_batches / self.batches if self.batches else 0.0

    @property
    def sentences_effective(self) -> int:
        """真正拿到响应的句数，用于密度与成本外推。

        被限流打掉的批次没有产出，若仍按送出的句数算密度会低估模型。
        """
        if self.batches <= 0:
            return self.sentences_sent
        return round(self.sentences_sent * self.batches_attempted / self.batches)

    @property
    def rows_written(self) -> int:
        """**交付出去的数据行数** —— 报给对方的必须是这一个。

        ⚠ 与 `terms_kept` **不是同一个数**：语料里有字节完全相同的文件（同一内容被
        上传多次，或我们归一化后内容相同）。md5 去重只调一次 API，但**每个输入文件
        各交一份 xlsx**，于是孪生文件的行被重复写出，而 `terms_kept` 只数一次。
        两者之差 = 被重复交付的那几份的行数之和。

        请拿这个数与 531 个 xlsx 的实际数据行总和核对 —— 它们必须相等。
        （2026-09-19 审计：README 报的 70,941 比 xlsx 实际的 71,097 少 156，
        就是那两个孪生文件各 78 行。对方一数就会发现，所以必须报实际行数。）
        """
        return sum(self.per_file.values())

    @property
    def anchor_pass_rate(self) -> float:
        return self.terms_kept / self.terms_proposed if self.terms_proposed else 0.0

    @property
    def refusal_rate(self) -> float:
        return self.refused / self.calls if self.calls else 0.0

    @property
    def terms_per_sentence(self) -> float:
        n = self.sentences_effective
        return self.terms_kept / n if n else 0.0

    @property
    def cache_ratio(self) -> float:
        return self.cached_tokens / self.prompt_tokens if self.prompt_tokens else 0.0

    @property
    def nom_total(self) -> int:
        return self.nom_ok + self.nom_fallback

    @property
    def nom_fallback_rate(self) -> float:
        """回落率：模型给的词典形没过校验、只能交原句切片的比例。

        这是俄语这一轮的新质量指标。**回落不丢术语**（召回优先），
        但回落率高就说明还原口径没落地，要回头改提示词而不是放宽校验。
        """
        return self.nom_fallback / self.nom_total if self.nom_total else 0.0

    def extrapolate(self, target_sentences: int) -> float:
        """按本次实测把成本外推到 target_sentences 句（预算的唯一可信来源）。

        分母用「真正拿到响应的句数」，否则被限流的运行会低估单句成本。
        """
        n = self.sentences_effective
        if n == 0:
            return 0.0
        return self.cost / n * target_sentences

    # ------------------------------------------------------------ 硬门槛

    def gate_verdict(self) -> list[str]:
        """计划 §4.4 预注册的硬门槛，跑完照此裁决。

        质量门槛与「运行是否顺利」严格分开：限流/网络失败不算模型质量问题。
        """
        v = []
        if self.morph_enabled is False:
            v.append(
                "淘汰：词典形语种但**形态分析器未启用** —— 校验只跑了不依赖词典的前几条判据，"
                "「压根没还原」与「方向搞反」会被放过并写进第 8/9 列（词条形式），"
                "`nom_plural_kept` 恒为 0。"
                "装上 simplemma / pymorphy3 后重跑（缓存在，不重复花钱）")
        if self.batch_exceptions:
            v.append(
                f"⚠ {self.batch_exceptions} 批出现**本地异常**（非 API 错误）。"
                f"这类批已按失败计入，不影响其余批的交付，但要看 errors 里的类型")
        if self.call_failures:
            # ⚠ 「多为限流」是一句**归因**，而这里以前不看原因就说它、还建议降并发。
            #   2026-09-19 夜端到端实测：一个 403 余额不足的批被说成「多为限流」，
            #   而正确的处置是充值 —— 降并发一点用都没有。
            #   同族第三处：闸门/文案在归因，却没看原因。归因要跟着证据走。
            if self.balance_errors:
                _cause, _adv = "账户余额不足", "充值后重跑"
            elif self.rate_limited:
                _cause, _adv = "限流", "降并发后重跑"
            else:
                _cause, _adv = "原因见 errors 里的类型", "按原因处置后重跑"
            v.append(
                f"⚠ 运行不完整：{self.call_failures}/{self.batches} 批调用层失败"
                f"（{self.call_failure_rate:.0%}），{_cause}；**质量数字只代表剩下的"
                f"{self.batches_attempted} 批，不能与完整运行直接比**。"
                f"{_adv}（缓存续跑，已完成的批不重复花钱）"
            )
        if self.batches_attempted and self.json_ok_rate < 0.95:
            v.append(f"淘汰：JSON 解析成功率 {self.json_ok_rate:.1%} < 95%")
        if self.terms_proposed and self.anchor_pass_rate < 0.90:
            v.append(f"淘汰：逐字校验通过率 {self.anchor_pass_rate:.1%} < 90%")
        poli = self.per_layer.get("conf/poli")
        if poli and poli["sent"] and poli["refused"]:
            v.append(f"警告：conf/poli 有 {poli['refused']} 次拒答/空响应（一票否决项）")
        if self.skipped_by_budget:
            _why = self.stop_reason or "花费熔断"
            # ⚠ 建议要跟着原因走 —— 三种停止原因的处置完全不同：
            #     429 熔断   -> 降并发（调 --max-spend 没用）
            #     余额不足   -> 充值（降并发、调上限都没用）
            #     花费熔断   -> 调高 --max-spend
            if "余额不足" in _why:
                _fix = "充值后重跑同一条命令"
            elif "429" in _why:
                _fix = "降低 --concurrency 后重跑同一条命令"
            else:
                _fix = "调高 --max-spend 重跑同一条命令"
            v.append(
                f"淘汰：{_why}跳过了 {self.skipped_by_budget} 批 —— 这不是一次完整运行。"
                f"{_fix}（已完成的批走缓存，不重复花钱）")
        if self.rate_limited:
            v.append(f"警告：429 共 {self.rate_limited} 次 —— 并发可能超了服务商"
                     f"上限。有实测背书的值是 40（50 从没在付费口验过）")
        if self.balance_errors:
            v.append(f"淘汰：账户余额不足 {self.balance_errors} 次 —— 这一轮没跑完，"
                     f"且与质量无关。失败的批没进缓存，充值后重跑同一条命令"
                     f"即可精确续传（只花剩下那些批）")
        if not v:
            v.append("通过全部硬门槛")
        return v

    # ------------------------------------------------------------ 渲染

    def to_text(self) -> str:
        L = []
        a = L.append
        a("=" * 68)
        a(f"术语抽取运行报告  {self.started} -> {self.finished}")
        a("=" * 68)
        a(f"模型      : {self.model}   档位: {self.effort or '(默认)'}")
        a(f"提示词    : {self.prompt}  (hash {self.phash})")
        a(f"批大小    : {self.batch_size}   并发: {self.concurrency}")
        a("")
        a("--- 规模 ---")
        a(f"文件      : 总 {self.files_total}  实际调用 {self.files_called}"
          f"  产出 xlsx {self.files_written}  0 术语 {self.files_zero_terms}")
        a(f"句对      : 总 {self.sentences_total}  送模型 {self.sentences_sent}")
        a(f"批次      : {self.batches}   JSON 解析失败 {self.json_failed_batches}")
        # ⚠ 这四类以前只进 JSON、文本报告里看不见 —— 而人看的是文本。
        #   它们的共同后果是「那几句在 xlsx 里表现为没有术语」，必须摆在明面上。
        a(f"不完整批  : {self.incomplete_batches}（{self.incomplete_rate:.2%}）"
          f"= 调用失败 {self.call_failures} + 拒答 {self.refused_batches}"
          f" + JSON 死 {self.json_dead_batches} + 熔断跳过 {self.skipped_by_budget}")
        if self.json_dead_batches:
            a(f"  JSON 死拆分: 真解析不出 {self.parse_dead_batches}"
              f"（缓存已撤，重跑会重试） + 模型给合法空数组 "
              f"{self.empty_array_batches}（= 这批确实没术语，缓存保留）")
        # ⚠ 这一档**不进闸门**，但必须印出来 —— 它是「模型明确说这批没术语」，
        #   与「我们不知道有没有」是两回事。原先它连计数都没有，全量里 96 批
        #   完全不可见（2026-09-19 审计发现）。
        if self.empty_batches:
            a(f"模型说没术语: {self.empty_batches} 批（{self.sentences_empty} 句）"
              f"  —— 大多是真的没有；名单见 json 的 empty_batch_list（封顶 500）")
            a("   ⚠ 抽查提示：若某个文件**成片**落在这一档，先查上游句对是否对得上，")
            a("     再怀疑模型 —— 实测最大的一个正是「两侧断句不一致」。")
        if self.rate_limited:
            a(f"限流      : 429 共 {self.rate_limited} 次（按尝试计）"
              f"   有实测背书的并发是 40")
        if self.balance_errors:
            a(f"余额不足  : {self.balance_errors} 次 —— **这一轮没跑完**，"
              f"与质量无关；充值后重跑同一条命令即可精确续传")
        if self.bad_shape or self.no_terms_key:
            a(f"输出结构  : 畸形条目 {self.bad_shape}（已触发纠正重试）"
              f"   无 terms 键 {self.no_terms_key}（= 该句没术语，不算错）")
        if self.delivery_normalized or self.dict_form_unified:
            a(f"交付侧收口: 标点归一化 {self.delivery_normalized} 处"
              f"   词典形统一 {self.dict_form_unified} 处（都不是错误数）")
        a("")
        a("--- 调用 ---")
        a(f"API 调用  : {self.calls}   缓存命中 {self.cache_hits}"
          f"   失败 {self.call_failures}   纠正重试 {self.correction_retries}")
        a(f"拒答/空   : {self.refused}  ({self.refusal_rate:.2%})"
          f"   截断(length) {self.truncated}")
        if self.calls:
            a(f"平均延迟  : {self.latency_sum / self.calls:.2f}s")
        a("")
        a("--- token 与成本 ---")
        a(f"输入      : {self.prompt_tokens:,}  其中缓存命中 {self.cached_tokens:,}"
          f"  ({self.cache_ratio:.1%})")
        a(f"输出      : {self.completion_tokens:,}  其中思考 {self.reasoning_tokens:,}")
        if self.completion_tokens:
            a(f"思考占输出: {self.reasoning_tokens / self.completion_tokens:.1%}")
        a(f"累计成本  : ${self.cost:.4f}   （含缓存命中的批，= 整轮规模的成本）")
        a(f"本轮新花  : ${self.new_spend_usd:.4f}  ← **报预算用这个数**"
          f"（只算真的发出去的调用；断点续跑时两者不同）")
        if self.sentences_sent:
            a(f"单句成本  : ${self.cost / self.sentences_sent:.6f}")
            name, n = FULL_CORPUS.get(self.lang, ("西语", 56110))
            if n:
                a(f"外推{name}全量 {n:,} 句: ${self.extrapolate(n):.2f}")
            else:
                a(f"{name}全量未交付，不外推；单价 ${self.cost / self.sentences_sent * 1000:.3f}/千句")
        a("")
        a("--- 质量 ---")
        a(f"JSON 成功率    : {self.json_ok_rate:.2%}")
        a(f"逐字校验通过率 : {self.anchor_pass_rate:.2%}"
          f"  (模型给 {self.terms_proposed} 条，留下 {self.terms_kept} 条)")
        # ⚠ 交付行数**必须单独印**：它与 `terms_kept` 在有重复输入文件时不相等
        #   （孪生文件各交一份 xlsx，行被重复写出）。报给对方的、能被对方数出来的
        #   是这个数，不是 terms_kept。（2026-09-19 审计：西语差 156。）
        if self.rows_written != self.terms_kept:
            a(f"交付数据行     : {self.rows_written} 行"
              f"（比 terms_kept 多 {self.rows_written - self.terms_kept} 行 —— "
              "重复输入文件各交了一份表）")
        a(f"锚定失败       : src {self.bad_anchor_src}  tgt {self.bad_anchor_tgt}")
        a(f"类型非法降级   : {self.bad_types}   sent_id 不在批内: {self.bad_sent_id}")
        a(f"术语密度       : {self.terms_per_sentence:.2f} 个/句")
        if self.nom_total:
            from .extract import morph_status
            a("")
            a("--- 词典形还原（ISO 10241-1 / IATE 口径）---")
            a(f"形态分析器     : {morph_status(self.lang)}")
            # ⚠ 措辞（2026-09-19 改）：句中切片从这一版起**始终**在交付表的第 4/5 列，
            #   所以「回落到原句切片」不再描述交付物长什么样，只描述第 8/9 列
            #   （词条形式）这一列有没有拿到词典形。别让「回落」听起来像降级。
            a(f"词典形还原成功 : {self.nom_ok}"
              f"   第 8/9 列与句中切片相同: {self.nom_fallback}"
              f"   （{self.nom_fallback_rate:.2%}）")
            a(f"其中真的改了形 : {self.nom_changed}"
              f"（占成功的 {self.nom_changed / self.nom_ok:.1%}）" if self.nom_ok
              else "")
            a(f"补语的数被改了 : {self.nom_comp_changed}"
              f"  ← 校验器**抓不到**这类错，只能人看。补语表构成实体该留复数"
              f"（immeuble de bureaux），表类别才单数（forme d'entreprise）")
            for w in self.nom_comp_notes[:10]:
                a(f"    - {w}")
            if len(self.nom_comp_notes) > 10:
                a(f"    …… 另有 {len(self.nom_comp_notes) - 10} 条（全量在 json）")
            a(f"其中交付值无损失（词典形与切片本来相同）: {self.nom_reject_same}"
              f"  -> 真实损失 {self.nom_fallback - self.nom_reject_same}")
            a(f"还原后仍是复数 : {self.nom_plural_kept}"
              "  ← 应当全是「习惯复数」或专名，**要人工抽看**（程序判不了「习惯」）")
            a("  回落**不丢术语**：该行照旧交付，只是术语列仍是句中形式")
            for e in self.nom_failures[:15]:
                a(f"    - {e}")
            if len(self.nom_failures) > 15:
                a(f"    …… 另有 {len(self.nom_failures) - 15} 条，见 json 报告")
        a("")
        a("--- 分层 ---")
        for layer in sorted(self.per_layer):
            d = self.per_layer[layer]
            dens = d["terms"] / d["sent"] if d["sent"] else 0
            a(f"  {layer:12s} 文件 {d['files']:4d}  句 {d['sent']:6d}"
              f"  术语 {d['terms']:6d}  密度 {dens:.2f}  拒答 {d['refused']}")
        a("")
        a("--- 预注册硬门槛裁决 ---")
        for v in self.gate_verdict():
            a(f"  * {v}")
        if self.corpus_issues:
            a("")
            a("--- 上游语料问题（§2 异常处置）---")
            for k, v in self.corpus_issues.items():
                a(f"  {k}: {v}")
        if self.errors:
            a("")
            a(f"--- 错误 ({len(self.errors)}) ---")
            for e in self.errors[:20]:
                a(f"  {e}")
        return "\n".join(L)

    def save(self, out_dir: Path, tag: str) -> tuple[Path, Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        txt = out_dir / f"report_{tag}.txt"
        txt.write_text(self.to_text(), encoding="utf-8")
        d = {k: v for k, v in self.__dict__.items() if k != "per_layer"}
        d["per_layer"] = {k: dict(v) for k, v in self.per_layer.items()}
        # ⚠ 两个列表都不设上限地 append，全量 18,605 批时能长到几万条、报告 json 变几 MB。
        #   保留计数 + 前 N 条明细；要全量明细去 terms_raw.json 与 stdout。
        for key, cap in (("nom_failures", 500), ("errors", 500),
                         ("nom_comp_notes", 500), ("empty_batch_list", 500)):
            full = list(getattr(self, key) or [])
            d[key] = full[:cap]
            d[f"{key}_total"] = len(full)
            if len(full) > cap:
                d[key].append(f"…… 另有 {len(full) - cap} 条已省略（共 {len(full)} 条）")
        d["derived"] = {
            "cost_usd": round(self.cost, 6),
            # ⚠ 口径警告（2026-09-19 审计）：`cost_usd` 是**价目表价**、含缓存命中，
            #   而 `new_spend_usd` 才是**本轮真花掉的钱**。续跑时前者能比后者大 3 倍
            #   （西语实测 $123.60 vs $37.04）。**报预算用 new_spend_usd。**
            #   文本报告里标了，机读 JSON 里以前没标，容易被下游直接取错。
            "cost_usd_note": "价目表价、含缓存命中；报预算请用 new_spend_usd",
            # 交付后核 xlsx 用这个，不要用 nom_changed（那是收口前的数）。
            "rows_dict_changed": self.rows_dict_changed,
            "rows_dict_changed_note": ("第 8/9 列 != 第 4/5 列的行数（收口后，可与 xlsx 直接核）；"
                                       "nom_changed 是收口前的数，两者本就不等"),
            "json_ok_rate": self.json_ok_rate,
            "anchor_pass_rate": self.anchor_pass_rate,
            "refusal_rate": self.refusal_rate,
            "terms_per_sentence": self.terms_per_sentence,
            "cache_ratio": self.cache_ratio,
            "lang": self.lang,
            # 全量未交付的语种（句数为 None）不外推，只给单价。
            "extrapolated_full_sentences": FULL_CORPUS.get(self.lang, ("西语", 56110))[1],
            "extrapolated_full_cost": (
                round(self.extrapolate(_full_n), 2)
                if (_full_n := FULL_CORPUS.get(self.lang, ("西语", 56110))[1]) else None),
            "cost_per_1k_sentences": (
                round(self.cost / self.sentences_sent * 1000, 4)
                if self.sentences_sent else None),
            "nom_fallback_rate": self.nom_fallback_rate if self.nom_total else None,
            "nom_changed_rate": (self.nom_changed / self.nom_ok
                                 if self.nom_ok else None),
            "nom_plural_kept": self.nom_plural_kept if self.nom_total else None,
            "nom_reject_same": self.nom_reject_same if self.nom_total else None,
            "nom_comp_changed": self.nom_comp_changed if self.nom_total else None,
            "new_spend_usd": round(self.new_spend_usd, 6),
            "morph_enabled": self.morph_enabled,
            "batch_exceptions": self.batch_exceptions,
            "gate_verdict": self.gate_verdict(),
        }
        js = out_dir / f"report_{tag}.json"
        js.write_text(json.dumps(d, ensure_ascii=False, indent=2, default=str),
                      encoding="utf-8")
        return txt, js
