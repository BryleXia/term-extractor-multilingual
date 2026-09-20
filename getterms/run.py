"""CLI：跑术语抽取。

冒烟（不花钱）：
  python -m getterms.run --model gemini-3.7-flash-free --effort medium \
      --limit-files 1 --limit-sentences 20 --tag smoke

西语全量：
  python -m getterms.run --model gemini-3.7-flash --effort medium \
      --batch-size 10 --concurrency 50 --out out/es --tag es_full_v1
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

from .config import (OUT_DIR, ROOT, corpus_dir, cost_usd, key_fingerprint,
                     load_api_key, utf8_stdout)
from .corpus import CorpusFile, load_corpus
from .extract import (DICT_FORM_LANGS, load_lemmatizer, load_morph, load_prompt,
                      make_batches, morph_status, normalize_delivery, run_batch,
                      unify_dict_forms)
from .llm import LLMClient
from .report import FULL_CORPUS, RunReport
from .writer import (DEFAULT_EXPORT, pack_zip, write_field_spec, write_readme,
                     write_xlsx)


def build_args(argv=None):
    p = argparse.ArgumentParser(prog="getterms.run", description="术语抽取流水线")
    p.add_argument("--model", required=True)
    p.add_argument("--effort", default=None,
                   help="该模型自己的 reasoning_effort 档位；不给则用模型默认档（注意默认档常常最贵）")
    p.add_argument("--prompt", default=None,
                   help="默认按语种取 <lang>_v3 / <lang>_v1")
    p.add_argument("--batch-size", type=int, default=10,
                   help="默认 10 = 我们的既定口径（全量预算、质量基线、缓存键都按 10 定的）"
                        "；下游 HTML 工具的默认值是 5")
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--out", default=None, help="默认 out/<lang>")
    p.add_argument("--lang", default="es", help="es / fr / ru，决定语料目录与默认提示词")
    p.add_argument("--tag", default=None, help="报告文件名标签")
    p.add_argument("--limit-files", type=int, default=0, help="0 = 不限")
    p.add_argument("--limit-sentences", type=int, default=0,
                   help="每文件最多送多少句，0 = 不限")
    p.add_argument("--files", default=None, help="文件名子串过滤")
    p.add_argument("--layer", default=None, help="scene/domain 过滤，如 conf/poli")
    p.add_argument("--sample-per-layer", type=int, default=0,
                   help="bake-off 用：每个 scene/domain 层最多取多少句")
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--no-correct-retry", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="只算工作量与预估，不调 API")
    p.add_argument("--no-zip", action="store_true")
    p.add_argument("--no-raw-dump", action="store_true",
                   help="不落盘 terms_raw.json（默认落盘，term_qc 要靠它溯源）")
    p.add_argument("--allow-degraded", action="store_true",
                   help="词典形语种缺形态分析器时仍然开跑（默认拒绝：校验会静默变宽松）")
    p.add_argument("--force", action="store_true",
                   help="out 目录已有 *term.xlsx 时仍然开跑（默认拒绝：陈旧文件污染 verify）")
    p.add_argument("--allow-incomplete", action="store_true",
                   help="不完整批超阈值时仍然产出 results.zip 与 README.md（默认拒绝）")
    p.add_argument("--max-spend", type=float, default=0.0,
                   help="本轮新花费上限（美元）。0 = 按本次工作量外推 x1.5 自动定。"
                        "越线立即停止派发新批，已完成的成果照样落盘。")
    p.add_argument("--allow-partial-corpus", action="store_true",
                   help="选中句数与登记的全量句数差超过 1%% 时仍然开跑（默认拒绝）"       # argparse 会把 %% 当格式符，裸 % 会让 --help 直接崩（bakeoff 踩过）
                   )
    p.add_argument("--yes-slow", action="store_true",
                   help="确认接受低并发带来的长墙钟（否则全量意图下并发低于 "
                        "50 且预估 >3 小时会停）。")
    p.add_argument("--allow-no-balance", action="store_true",
                   help="账户余额不足时也继续派发新批（默认：第一次命中就停止派发，"
                        "因为后面每一批都会同样失败）")
    p.add_argument("--allow-429", action="store_true",
                   help="被限流也继续派发新批（默认：429 达到 20 次且超过已完成"
                        "调用数的 10%% 就停止派发，避免整轮被打废）")
    p.add_argument("--skip-preflight", action="store_true",
                   help="跳过缓存/磁盘/API key 探针（默认都做，它们合计不到 2 秒）")
    return p.parse_args(argv)


# 失败批闸门：超过这个比例就不产出交付包。xlsx 照旧写出，不受影响。
INCOMPLETE_GATE = 0.002

# 给下游工具方的交付说明 —— **随 zip 一起发**。仓库根目录下这一份。
# 它装着「上游数据里需要你知道的问题」全套（sent_id 重编的 12 个文件、异种 schema、
# 键盘乱敲、字段错位……），而这些问题**只在我们这边的机读报告里**，对方看不见。
DELIVERY_NOTE_NAME = "交付说明_给下游工具方.md"


# ⚠ 三语都指向带 `dict_form` 输出键的版本（2026-09-18 起，ISO 10241-1 / IATE 口径）。
#   改这里不影响 bakeoff 文件名签名里那个硬写的「无后缀基线」
#   （bakeoff.py 的 _default_prompt），那是故意的。
# fr_v6（2026-09-18）：法语老师对 fr_v5 小样的反馈里追加了三条补语规则 ——
#   ① 机构专名/传统固定复数原样保留 ② 普通名词定语表类别，后置名词单数
#   ③ de 后面表示「由多个实体构成」则保留复数（groupement d'entreprises）
# fr_v5 那句「从属成分照抄句中形式」与 ② 相反，故换版。
# 同一份 490 句样本实测：术语 687 vs 690（-0.4%，噪声带内）、回落 0.00%、
#   `补语的数被改了` = 0、③ 类 17 条补语全部保住、7 条「仍为复数」人工看过 7/7 正确
#   （fruits de mer / vacances du Nouvel An / îles Penghu / monts Qilian 等）。
# ⚠ **不要**用「两版逐条 diff」去归因：那是 n=1 抽样。实测 `Dans les temps anciens.`
#   在 fr_v5 下 --no-cache 跑 3 次给出 3 个不同结果（含整条漏抽），fr_v6 跑 3 次 3/3 一致。
#   单条术语的值本身就在噪声里，见 memory: prompt_ab_noise_floor。
# ---------------------------------------------------------------- 2026-09-18 下半场
# es_v7 / fr_v7 / ru_v6：三语同一处根因 —— 提示词把 IATE 手册 §12.3.3 的
#   "except where the term is habitually used in the plural"
# 收窄成了 "only exists in the plural"。后者严得多：`precipitación`、`objectif`、
# `осадок` 的单数在语法上全部合法存在，于是模型把「大气降水」「可持续发展目标」
# 压成单数 —— 那是**在照字面执行我们自己写错的规则**。改动只有三处（A1 判据改回原话、
# A2 给中心词补「拿不准就保留句中形式」的兜底、A3 用实证词补齐例子）。
#
# 每臂 n=2 独立采样实测（同一份既有样本，新提示词，$9.55）：
#   西语 惯用复数被压单数 9 -> 0/0     过度纠正 0   新出现 0   两臂逐条不一致 0
#   法语                10 -> 3/3     过度纠正 0   新出现 0   两臂逐条不一致 0
#   俄语（全量语料样本）  1 -> 0/0     过度纠正 2（不在清单里，已列表问老师）
#   三语 JSON 100% / 逐字锚定 100% / 拒答 0 / 截断 0 / 条数变动 −1.5%~+1.0%（噪声带内）
#   span 缺陷 10 类全部在 ±3 条内（改动没碰抽取形状）
# 法语剩下的 3 条是同一份清单的过度匹配（`sources d'eau`）与「弱条目」（`vêtements`），
#   已放进确认表问老师，不是模型不听话。
# ------------------------------------------------------------ 2026-09-18 收口
# 西语再前移一版：es_v7 里「保留复数」的示例表有一行是**我自己写错的** ——
#   `precipitaciones atmosféricas`。四条证据同向说明西语该用单数：
#   IATE 条目 45260 词条形 `precipitación atmosférica`；AEMET《Manual de uso de
#   términos meteorológicos》把气象**变量**列作单数 `precipitación`；DLE 气象义
#   单数立目且**没有** `U. m. en pl.` 标记（对比 `masa` 义 9、`elección` 义 4、
#   `palillo` 义 12 都有明确的复数标记 —— 没标记就是反面证据）。
# ⚠ 并撤掉我原先那条论据：「`precipitaciones`(es) 与 `précipitations`(fr) 同词同错」。
#   法语那侧**该用复数**（Larousse 另立 `précipitations, n. f. pl.` 独立词条）。
#   同一概念在两语的术语惯例可以不同，**跨语种类推不能当证据** —— 这就是活例子。
# es_v8 只改三处（删该示例、警告换成 DLE 查实的 palillo/masa、示例表换成 palillos），
#   其余逐字沿用 es_v7。法语与俄语提示词**一字未动**（引用的例子全部查实正确），
#   所以 fr/ru 的缓存全部命中、重跑 $0。
DEFAULT_PROMPT = {"es": "es_v8", "fr": "fr_v7", "ru": "ru_v6"}


# 三语共用的交付口径说明，进 README。
DICT_FORM_CALIBER = """
## 术语的**词条形式**收词典形（ISO 10241-1 / IATE 口径）

表里有两列装术语：**第 4/5 列** `term_src`/`term_tgt` 装**句中原始切片**
（原句里怎么写就怎么抄）；**第 8/9 列** `term_src_dict`/`term_tgt_dict` 装
**词条形式**。这一节说的是后者。

第 8/9 列对**外语那一侧**收的是术语的**原型**，不是讲话人当时用的那个屈折形式。
依据是国际术语工作规范，不是我们自己定的：

- **ISO 10241-1:2011 §6.2.2**：术语以基本语法形式给出 —— 名词用**单数**，
  形容词（屈折语言）用未屈折形式，动词用不定式。
- **IATE 手册 §12.3.3**（欧盟 26 语种术语库）：名词和形容词写单数，
  **除非该术语习惯上以复数使用**；不加冠词；用该语言的规范形式（如主格）。
- **ISO 10241-1 §6.2.9.3.1**：有性的语言**标注**名词的性 —— 性是词条属性，
  不是被规范掉的东西。所以**名词的性我们绝不改**（`la política` 政策 与
  `el político` 政客 是两个词）。

具体到三个语种：

| 轴 | 处理 |
|---|---|
| 格（只有俄语有） | 中心词组还原到主格；从属成分保留自己的格（`империя лжи`） |
| 数 | 中心名词默认**单数**；**习惯复数**（`derechos humanos`、`droits de l'homme`、`права человека`、`совместные учения`）、专名（`Estados Unidos`、`Nations unies`）、缩写**保留原样** |
| 性 | 名词的性不动；修饰语与中心名词一致（`empresa privada`，不是 `empresa privado`） |
| 中文侧 | 无屈折，第 8/9 列与第 4/5 列相同 |

⚠ **为什么第 4/5 列必须是句中原始切片**：转成 `final.json` 时要把术语**内联**标进
句子（`当前[世界经济]{term}面临…`），那一步得拿这一列去原句里**定位**。
所以它逐字不动，**也不做标点归一化**（撇号 / 外层引号的归一化结果落在第 8/9 列）。

逐字锚定的闸门没有拆，只是换成两段式：

1. 模型仍必须先给出能在原句里逐字定位的切片，锚不上就丢弃；
2. 锚定通过后，模型另给的 `dict_form` 要过程序校验（词数、功能词、专名、字符集、
   词元共享、不得复数化、性别标记不许翻转；俄语再加中心组主格）才被采用；
3. 校验不过的**不丢术语**，第 8/9 列回落到与第 4/5 列相同，回落条目与原因都在报告里。

句中原始切片两处都有：**交付 xlsx 的第 4/5 列**，以及 `terms_raw.json` 的
`term_*_span` 字段；另有 `python -m getterms.qc_workbook out/<语种>` 生成的核对
工作簿（**不进交付 zip**）。
"""

# 旧名保留（外部脚本可能引用）
RU_CALIBER = DICT_FORM_CALIBER


def select_files(files: list[CorpusFile], a) -> list[CorpusFile]:
    sel = files
    if a.files:
        sel = [f for f in sel if a.files in f.basename]
    if a.layer:
        sel = [f for f in sel if f.layer() == a.layer]
    sel = sorted(sel, key=lambda f: f.basename)
    if a.limit_files:
        sel = sel[: a.limit_files]
    return sel


def plan_batches(reps: list[CorpusFile], a) -> tuple[list[tuple], dict]:
    """把要跑的批次摊平成任务列表。返回 (任务, 分层配额用量)。

    任务项 = (file, batch_idx, batch)
    """
    tasks: list[tuple] = []
    layer_used: dict[str, int] = {}
    for f in reps:
        us = f.usable_sentences
        if a.limit_sentences:
            us = us[: a.limit_sentences]
        if a.sample_per_layer:
            lay = f.layer()
            room = a.sample_per_layer - layer_used.get(lay, 0)
            if room <= 0:
                continue
            us = us[:room]
            layer_used[lay] = layer_used.get(lay, 0) + len(us)
        if not us:
            continue
        batches = [us[i:i + a.batch_size] for i in range(0, len(us), a.batch_size)]
        for i, b in enumerate(batches):
            tasks.append((f, i, b))
    return tasks, layer_used


def _auto_budget(a, rep) -> float:
    """按本次工作量外推 x1.5 的花费上限。

    单价取三份定稿报告的实测区间上沿（$2.73 / 千句，gemini-3.7-flash @ high，
    批 10）。批大小偏离 10 时按「系统提示词多发几遍」线性修正 —— 批越小、
    每句摊到的系统提示词越多。x1.5 是给思考 token 方差留的余量
    （reasoning 占 completion 的 77%）。
    """
    per_1k = 2.73
    scale = (10.0 / a.batch_size) if a.batch_size else 1.0
    est = rep.sentences_sent / 1000.0 * per_1k * max(1.0, scale)
    return round(max(1.0, est * 1.5), 2)


# 单批墙钟。取六次生产提示词实测（gemini-3.7-flash @ high、批 10、并发 12）的
# **上沿**：es_v8 37.36/41.63s、fr_v7 35.18/38.79s、ru_v6 33.63/41.59s，
# 均值 38.0s、区间 33.6~41.6s。与 _auto_budget 取单价上沿同一个口径 —— 预估用来
# 拦「忘了给参数」，宁可报得比实际慢。
SECONDS_PER_BATCH = 42.0

# 全量口径的并发。低于它会让墙钟成倍上去，而这件事以前没有任何提示。
# ⚠ 2026-09-19 夜从 50 降到 40：**40 是唯一有实测背书的值**
#   （bakeoff/report_..._fr__fr_v8__g3*.json 两臂各 50 批、call_failures=0）。
#   50 只验到客户端侧能跑起来，**付费口会不会 429 从没验过**
#   （计划_俄语.md:564-568 自己写着「还没验到的」）；profiles.py 那句
#   「aihubmix 明示不限并发；从 50 起步，10 分钟无 429 再翻倍」是当初写的
#   **计划**，而那个斜坡从来没实现。
#   代价：三语墙钟 4.4h -> 5.4h（仍在一天内）。换掉的是「整轮被 429 打废」
#   —— 智谱直连并发 12 就 26/44 批 429、整轮作废（profiles.py:32-34）。
#   这是**地板不是天花板**：仍可显式给 50，但那是没有证据的一步。
FULL_CONCURRENCY = 40


def _eta_hours(batches: int, concurrency: int) -> float:
    """墙钟预估（小时）。批之间是纯 IO 等待，所以除以并发即可。"""
    return batches * SECONDS_PER_BATCH / max(1, concurrency) / 3600.0


# 429 熔断的阈值。绝对量避开冷启动抖动，占比避开「跑了很久偶尔几次」的误杀。
RATE_LIMIT_MIN_HITS = 20
RATE_LIMIT_MAX_SHARE = 0.10


def _rate_limit_tripped(n429: int, calls: int) -> bool:
    """限流是否已经严重到该停止派发新批。

    为什么是纯函数而不是埋在闭包里：熔断第一版的教训不是逻辑写错，而是
    **「断言过了、实测无效」**（判断放在 worker 开头，而 asyncio.gather 把全部协程
    在 t=0 一起启动，它们都在还没花钱的时刻通过了判断）。埋在闭包里的判据，
    外部只能读源码字符串来断言 —— 那种断言证明不了任何行为。

    `n429` 按**尝试**计（一批重试两次撞两次 429 就是 2），`calls` 是已完成的调用数。
    两个条件同时满足才触发：绝对量 >= 20（避开冷启动抖动），且超过已完成调用的 10%
    （避开「跑了几千批偶尔几次」的误杀）。
    """
    return n429 >= RATE_LIMIT_MIN_HITS and n429 > RATE_LIMIT_MAX_SHARE * max(1, calls)


async def _preflight(a, client, out_dir) -> int:
    """开跑前的三个探针：缓存可写、out 可写且空间够、API key 与模型真的能用。

    返回 0 = 放行，非 0 = 停止。三者都是「不做就要跑完几小时才发现」的类型。
    """
    import shutil

    # ⑤ 缓存目录可写。_write_cache 吞 OSError（已补计数，但那是跑完才看见）。
    try:
        client.cache_root.mkdir(parents=True, exist_ok=True)
        _probe = client.cache_root / ".write_probe"
        _probe.write_text("ok", encoding="utf-8")
        _probe.unlink()
    except OSError as e:
        print(f"\n[停止] 缓存目录不可写：{client.cache_root}\n"
              f"        {type(e).__name__}: {e}\n"
              "        不修就是 18,600 批一个都不缓存，任何一次中断都要全价重跑。")
        return 2

    # ⑥ out 目录可写 + 剩余空间。实测全量占用 < 0.5 GB，门槛 2 GB 有充分余量。
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        _probe = out_dir / ".write_probe"
        _probe.write_text("ok", encoding="utf-8")
        _probe.unlink()
    except OSError as e:
        print(f"\n[停止] 输出目录不可写：{out_dir}\n        {type(e).__name__}: {e}")
        return 2
    free_gb = shutil.disk_usage(out_dir).free / 1024 ** 3
    print(f"磁盘剩余 {free_gb:.1f} GB（全量实测占用 < 0.5 GB）")
    if free_gb < 2.0:
        print("\n[停止] 剩余空间不足 2 GB。跑完才在 write_xlsx 上撞见"
              "= 钱花完、一个 xlsx 都没有。")
        return 2

    # ⑦ API key 与模型可用性。一次 1 句的真实请求（几分之一美分），
    #    挡的是「key 过期 / 模型名写错 / 供应商下线」导致 18,600 批全失败。
    #    ⚠ 走独立的 batch_idx 命名空间，不污染正式批的缓存。
    try:
        r = await client.call(
            [{"role": "user", "content": "Reply with the single word: ok"}],
            "0" * 32, 99_999, "preflight")
    except Exception as e:  # noqa: BLE001
        print(f"\n[停止] API 探针抛异常：{type(e).__name__}: {e}")
        return 2
    if not r.ok:
        print(f"\n[停止] API 探针失败：{r.error}\n"
              f"        key 指纹 {key_fingerprint(load_api_key(client.profile.provider.key_file))}"
              f"  model id {client.profile.model}\n"
              "        先修这个，否则 18,600 批会全部失败、白排几小时队。")
        return 2
    print(f"API 探针: ok（{r.latency_s:.1f}s，"
          f"{'缓存命中' if r.from_cache else '真实请求'}）")
    return 0


async def main_async(a) -> int:
    out_dir = Path(a.out) if a.out else OUT_DIR / a.lang
    tag = a.tag or f"{a.model.replace('.', '_')}_{a.effort or 'default'}_b{a.batch_size}"

    prompt = a.prompt or DEFAULT_PROMPT.get(a.lang, f"{a.lang}_v1")
    system, user_template, phash = load_prompt(prompt)
    print(f"提示词 {prompt}  hash={phash}  system {len(system)} 字符")

    # ⚠ 形态分析器的状态必须在**开跑前**摆出来。以前它只出现在最后的报告里，
    #   而缺库是静默降级：校验只跑不依赖词典的前几条判据，「压根没还原」与
    #   「方向搞反」会被放过并写进交付列，nom_plural_kept 恒为 0，
    #   gate 照印「通过全部硬门槛」—— 换个 Python 环境跑 5,852 批，
    #   要等钱花完才看得出来。这里顺便预热词典，早失败。
    morph_enabled = None
    if a.lang in DICT_FORM_LANGS:
        morph_enabled = (
            load_morph() if a.lang == "ru" else load_lemmatizer()) is not None
        print(f"形态分析器 {morph_status(a.lang)}")
        if not morph_enabled and not a.allow_degraded:
            print("\n[停止] 该语种要交词典形，但形态分析器没启用。此时校验会静默放过\n"
                  "        「压根没还原」与「方向搞反」，并把它们写进交付列。\n"
                  "        先装依赖：pip install simplemma pymorphy3 pymorphy3-dicts-ru\n"
                  "        确认要带着降级跑，再加 --allow-degraded。")
            return 2

    cdir = corpus_dir(a.lang)
    print(f"语料目录 {cdir}")
    files, skipped = load_corpus(cdir)
    all_sel = select_files(files, a)

    # ⚠ 输出名碰撞会让后写的 xlsx **直接覆盖**先写的，且 files_written 多报、
    #   zip 里同名成员写两遍。replace_out_name 天然多对一（x.json 与 x.jsonl 撞、
    #   拼错名归一化后也可能与规范名撞）。以前只有法语的测试里有这条断言。
    _names = [f.out_name for f in all_sel]
    if len(set(_names)) != len(_names):
        import collections as _c
        dups = {k: v for k, v in _c.Counter(_names).items() if v > 1}
        print(f"\n[停止] {len(dups)} 组输出名碰撞，后写的会覆盖先写的：")
        for k, v in list(dups.items())[:10]:
            src = ", ".join(f.basename for f in all_sel if f.out_name == k)
            print(f"        {k}  x{v}  <- {src}")
        return 2

    # md5 去重：每组只调一次 API，其余成员复用结果（§2 行 14）。
    # 代表必须**从选中的文件里**挑，否则会去调用用户没选的文件。
    # 用 basename 做键，避免 dataclass 的深比较（533×56k 字段比较很慢）。
    reps_by_md5: dict[str, CorpusFile] = {}
    for f in sorted(all_sel, key=lambda x: x.basename):
        reps_by_md5.setdefault(f.md5, f)
    reps = list(reps_by_md5.values())

    tasks, layer_used = plan_batches(reps, a)
    n_sent = sum(len(b) for _, _, b in tasks)
    print(f"选中文件 {len(all_sel)}（去重后调用 {len(reps)}）"
          f"  批次 {len(tasks)}  送模型句对 {n_sent}")
    if layer_used:
        print("  分层取样:", dict(sorted(layer_used.items())))

    rep = RunReport(model=a.model, effort=a.effort, prompt=prompt, phash=phash,
                    batch_size=a.batch_size, concurrency=a.concurrency, lang=a.lang)
    rep.files_total = len(all_sel)
    rep.files_called = len(reps)
    rep.sentences_total = sum(len(f.sentences) for f in all_sel)
    rep.sentences_sent = n_sent
    rep.batches = len(tasks)
    rep.morph_enabled = morph_enabled

    # ---- 校验④ 语料完整性。⚠ 以前 FULL_CORPUS 登记了审计过的可用句数，
    #      run.py 也打印了实际句数，**两者从不比对** —— 少一个 zip、语料目录被挪走、
    #      --files 打错，都会让全量跑在一个子集上并正常产出一个「完整」的交付包。
    #      只在没有任何取样/过滤参数（= 全量意图）时才判。
    # ⚠ FULL_CORPUS 的值是 (语种名, 句数) 元组，不是裸整数。取 [1]。
    #   （我第一版直接拿它做减法，TypeError —— 引用别处的名字必须先确认它的形状。）
    _fc = FULL_CORPUS.get(a.lang)
    _full = int(_fc[1]) if isinstance(_fc, (tuple, list)) and len(_fc) > 1 else 0
    _sampling = bool(a.limit_files or a.limit_sentences or a.files
                     or a.layer or a.sample_per_layer)
    if _full and not _sampling:
        # ⚠ 比的必须是 sentences_sent（可用句对、md5 去重后真的要调的），
        #   不是 sentences_total（含单侧空/纯数字/纯标点等不送模型的句子）。
        #   FULL_CORPUS 登记的就是 audit 实测的**可用**句对：三语此处应当精确相等
        #   （es 56110 / fr 65097 / ru 57469）。第一版拿 sentences_total 去比，
        #   俄语差 1.65% 被误停 —— 阈值没错，是我比错了量。
        _gap = abs(rep.sentences_sent - _full) / _full
        print(f"语料完整性: 实调 {rep.sentences_sent} 句 / 登记可用 {_full} 句"
              f"（差 {_gap:.2%}；另有 {rep.sentences_total} 句为选中文件的全部句数）")
        if _gap > 0.01 and not a.allow_partial_corpus:
            print(f"\n[停止] 选中句数与登记的全量句数差 {_gap:.2%}（阈值 1%）。\n"
                  f"        登记值在 report.FULL_CORPUS['{a.lang}'][1] = {_full}，"
                  "来自语料审计。\n"
                  "        常见原因：少解压一个 zip、语料目录被挪走、"
                  "--files 打错、语料确实换了。\n"
                  "        跑在子集上会**正常产出一个完整的交付包**，"
                  "这是最难发现的一类错。\n"
                  "        确认无误再加 --allow-partial-corpus；"
                  "语料真的变了就同步改 FULL_CORPUS。")
            return 2

    # ---- 校验③ 提前到 dry-run 之前。⚠ 以前 dry-run 在这一步之前就 return 0，
    #      于是 dry-run 报「一切正常」，真跑才被陈旧 xlsx 拦住。
    #      陈旧 xlsx 不会被清理，而 verify 是 glob 整个目录 —— 它会把上一轮
    #      `--limit-files` 留下的文件当本轮交付读进来，report 与 verify 的个数对不上，
    #      还违反「0 术语不产出 xlsx」的契约却查不出来。
    _stale = sorted(out_dir.glob("*term.xlsx")) if out_dir.exists() else []
    if _stale and not a.force:
        print(f"\n[停止] {out_dir} 里已有 {len(_stale)} 个 *term.xlsx"
              f"（如 {_stale[0].name}）。陈旧文件会污染 verify 的门禁结果。\n"
              "        请换一个 --out 目录、清空它，或确认后加 --force。")
        return 2

    # ---- 花费上限。0 = 按本次工作量外推 x1.5。外推的依据是 142 批样本
    #      （全量的 0.76%），而 reasoning_tokens 占 completion 的 77% ——
    #      思考 token 的方差就是预算的方差，所以必须有熔断。
    max_spend = a.max_spend if a.max_spend > 0 else _auto_budget(a, rep)
    # ⚠ `:g` 不用 `:.2f`：`:.2f` 会把 $0.005 印成 "$0.01"（比实际值大）。
    #   付费跑给的是 $11 / $229.77，两位小数够；但小上限会印出一个**不等于**
    #   实际上限的数，而这一行是操作者确认「闸门设对了没有」的依据。
    print(f"花费上限: ${max_spend:g}"
          + ("（--max-spend 指定）" if a.max_spend > 0 else "（按工作量外推 x1.5）"))

    # ---- 校验⑧ 墙钟。⚠ 上一轮只修了「批大小默认 5 而全量要 10」（那一半是钱），
    #      漏了同一条里的「并发默认 8 而全量要 50」（这一半是时间，且完全静默）。
    #      忘一个参数：单语种 1.4 h → 8.5 h，三语 4 h → 一整天。
    _eta = _eta_hours(rep.batches, a.concurrency)
    _eta_full = _eta_hours(rep.batches, FULL_CONCURRENCY)
    print(f"墙钟预估: {_eta:.1f} 小时（并发 {a.concurrency}，单批 "
          f"{SECONDS_PER_BATCH:.0f}s 取六次实测上沿）")
    if not _sampling and a.concurrency < FULL_CONCURRENCY and _eta > 3.0:
        print(f"\n[停止] 并发 {a.concurrency} 下这一轮要跑 {_eta:.1f} 小时；"
              f"全量口径是 --concurrency {FULL_CONCURRENCY}"
              f"（约 {_eta_full:.1f} 小时）。\n"
              f"        默认值 {a.concurrency} 是给冒烟用的，"
              "忘了改它不会有任何别的提示 —— 这是本轮第六次\n"
              "        撞上「默认值与全量口径不一致」。\n"
              f"        要么加 --concurrency {FULL_CONCURRENCY}，"
              "要么确认真的愿意慢跑再加 --yes-slow。")
        return 2

    if a.dry_run:
        print("\n[dry-run] 不调 API。工作量与上面的校验结果如上。")
        return 0

    client = LLMClient(a.model, a.effort, phash,
                       concurrency=a.concurrency, use_cache=not a.no_cache)
    prov = client.profile.provider
    print(f"API key: {key_fingerprint(load_api_key(prov.key_file))}"
          f"  provider={prov.name}  base={prov.base_url}")
    print(f"发给 API 的 model id: {client.profile.model}"
          f"   记账用: {client.price_key}")
    print(f"参数剖面: {client.wire_params()}")

    # ---- 校验⑤⑥⑦ 三个探针。合计不到 2 秒，挡的是「几小时白跑」。
    if not a.skip_preflight:
        _rc = await _preflight(a, client, out_dir)
        if _rc:
            return _rc

    rows_by_md5: dict[str, list] = {}
    done = 0

    async def _run_one(f: CorpusFile, idx: int, batch):
        oc, calls = await run_batch(
            client, system, user_template, batch, f.md5, idx,
            correct_retry=not a.no_correct_retry, lang=a.lang,
        )
        for c in calls:
            if c.from_cache:
                rep.cache_hits += 1
            elif c.ok:
                rep.calls += 1
            else:
                rep.calls += 1
                rep.call_failures += 1
                if c.error:
                    rep.errors.append(f"{f.basename} b{idx}: {c.error}")
            # 只有真的发出去的调用才算本轮新花的钱。缓存命中的 token 照旧累加进
            # prompt_tokens —— 那是为了让报告能说明整轮的规模。
            if not c.from_cache:
                rep.new_cost_usd += cost_usd(a.model, c.prompt_tokens,
                                             c.cached_tokens, c.completion_tokens)
            rep.prompt_tokens += c.prompt_tokens
            rep.cached_tokens += c.cached_tokens
            rep.completion_tokens += c.completion_tokens
            rep.reasoning_tokens += c.reasoning_tokens
            rep.latency_sum += c.latency_s
            if c.refused:
                rep.refused += 1
                rep.per_layer[f.layer()]["refused"] += 1
            if c.truncated:
                rep.truncated += 1
        if len(calls) > 1:
            rep.correction_retries += 1
        if oc.json_failed:
            rep.json_failed_batches += 1
        rep.bad_shape += oc.bad_shape
        rep.no_terms_key += oc.no_terms_key
        # ---- 这一批算不算「不完整」。三类在交付物里的表现完全一样：那几句看起来
        #      「没有术语」，而我们不知道它本该有没有。所以三类同权进闸门，
        #      并且都要能定位回原文（文件 + 批号 + 句号区间）。
        _span = (f"句 {batch[0].pos}~{batch[-1].pos}" if batch else "空批")
        _last = calls[-1] if calls else None
        if _last is not None and _last.ok and _last.refused:
            rep.refused_batches += 1
            rep.dead_batches_total += 1
            if len(rep.dead_batches) < 500:
                rep.dead_batches.append(
                    f"{f.basename} b{idx} {_span} 拒答/空响应"
                    f"（finish_reason={_last.finish_reason}）")
        elif oc.json_failed and _last is not None and _last.ok:
            # `not r.ok` 的批已经进 call_failures，不重复计
            rep.json_dead_batches += 1
            # ⚠ 两种成因，处置相反（见 report.py 的字段注释）：
            #   真解析不出来 -> 缓存**已被 run_batch 的 _finish() 撤掉**，
            #     重跑会重新调用；这是「重跑补齐失败批」真正生效的前提。
            #   模型给合法 `[]` -> 那是最终答案，缓存**保留**，
            #     否则每次重跑都为「这批确实没术语」重新付费。
            if oc.parse_failed:
                rep.parse_dead_batches += 1
                _dead_why = "纠正重试后仍解析不出 JSON 数组（缓存已撤，重跑会重试）"
            else:
                rep.empty_array_batches += 1
                _dead_why = "模型给了合法空数组 = 这批确实没术语（缓存保留）"
            rep.dead_batches_total += 1
            if len(rep.dead_batches) < 500:
                rep.dead_batches.append(
                    f"{f.basename} b{idx} {_span} {_dead_why}")
        rep.terms_proposed += oc.n_items
        rep.terms_kept += oc.n_kept
        # ⚠ **「这一批 0 术语」必须单独计数**（2026-09-19 全量审计发现的盲区）。
        #   报告原来把它彻底吞掉：`empty_array_batches` 只在模型回**裸 `[]`** 时才 +1，
        #   而「回了逐句对象、但每句的 `terms` 都是空数组」走的是**正常解析路径**，
        #   `terms_proposed += 0`，**任何计数器都不动**。
        #   后果：西语全量报告写着「不完整批 0（0.00%）」，而实测有
        #   **96 个批（1.64%）/ 605 句**是这种「模型说没有」—— 完全不可见。
        #   它大多是真的没术语，但也可能是模型在犯错或上游句对根本对不上
        #   （实测最大的一个就是后者）。**看不见就无法判断**，所以先让它可见。
        if oc.n_kept == 0 and not oc.failures:
            rep.empty_batches += 1
            rep.sentences_empty += len(batch)
            if len(rep.empty_batch_list) < 500:
                rep.empty_batch_list.append(
                    f"{f.basename} b{idx} 句 {batch[0].pos}~{batch[-1].pos}")
        rep.bad_anchor_src += oc.bad_anchor_src
        rep.bad_anchor_tgt += oc.bad_anchor_tgt
        rep.bad_types += oc.bad_types
        rep.bad_sent_id += oc.bad_sent_id
        rep.nom_ok += oc.nom_ok
        rep.nom_fallback += oc.nom_fallback
        rep.nom_changed += oc.nom_changed
        rep.nom_plural_kept += oc.nom_plural_kept
        rep.nom_reject_same += oc.nom_reject_same
        rep.nom_comp_changed += oc.nom_comp_changed
        rep.nom_comp_notes.extend(f"{f.basename} {x}" for x in oc.nom_comp_notes)
        rep.nom_failures.extend(f"{f.basename} {x}" for x in oc.nom_failures)
        rows_by_md5.setdefault(f.md5, []).extend(oc.rows)
        d = rep.per_layer[f.layer()]
        d["sent"] += len(batch)
        d["terms"] += oc.n_kept

    # `why` 会写进 rep.stop_reason 与跳过清单 —— 「调高 --max-spend」这条建议
    # 对 429 熔断是错的（那要降并发），所以原因必须一路带到报告里。
    stop_flag = {"hit": False, "why": ""}
    # ⚠ 熔断的检查必须放在**抢到派发槽之后**，不能放在 worker 开头。
    #   `asyncio.gather` 会把全部 18,600 个协程在 t=0 一起启动，它们各自跑到
    #   第一个挂起点（client 内部的信号量）才让出控制权 —— 也就是说所有协程都在
    #   还没花过一分钱的时刻通过了「已经超支了吗」这个判断，熔断等于不存在。
    #   实测（4 批 / 并发 2 / 上限 $0.01）：熔断打印了，但 4 批全跑完、花到 $0.1038。
    #   加一个派发信号量，把检查挪到拿到槽之后：在飞的批最多 concurrency 个，
    #   所以超支上限 = concurrency x 单批成本（并发 50 时约 $0.28）。
    dispatch_sem = asyncio.Semaphore(max(1, a.concurrency))

    async def worker(f: CorpusFile, idx: int, batch):
        """兜底壳。⚠ 以前 gather 里是裸的批处理：形态分析器 / 校验 / 类型归一化
        路径上任何一个本地异常都会冒到 asyncio.run，而外层只 except KeyboardInterrupt
        —— 第 5,800 批上抛一次，前面 5,799 批的结果**全丢、一个 xlsx 都不写**。
        现在按「这一批失败」记账，其余批照常交付。
        """
        nonlocal done
        async with dispatch_sem:
            if stop_flag["hit"]:
                rep.skipped_by_budget += 1
                # ⚠ 以前这里只有计数：跑完只知道「跳过 N 批」，不知道是哪 N 批，
                #   而这 N 批的句子在 xlsx 里和「真的没术语」一模一样。
                if len(rep.skipped_batches) < 500:
                    _sp = (f"句 {batch[0].pos}~{batch[-1].pos}"
                           if batch else "空批")
                    rep.skipped_batches.append(
                        f"{f.basename} b{idx} {_sp} "
                        f"{stop_flag['why'] or '熔断'}未派发")
                return
            await _dispatch(f, idx, batch)

    async def _dispatch(f: CorpusFile, idx: int, batch):
        nonlocal done
        try:
            await _run_one(f, idx, batch)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 —— 就是要兜住所有本地异常
            rep.calls += 1
            rep.call_failures += 1
            rep.batch_exceptions += 1
            rep.errors.append(
                f"{f.basename} b{idx}: 本地异常 {type(e).__name__}: {e}"[:500])
        finally:
            done += 1
            # ---- 花费熔断。越线后**停止派发新批**；已在飞的批不强杀 ——
            #      杀掉等于把已经花掉的钱扔了，而它们的响应还能进缓存供续跑复用。
            if rep.new_spend_usd > max_spend and not stop_flag["hit"]:
                stop_flag["hit"] = True
                stop_flag["why"] = "花费熔断"
                rep.stop_reason = "花费熔断"
                # ⚠ 上限用 `:g` 不用 `:.2f`：`:.2f` 会把 $0.005 印成 "$0.01" ——
                #   一句「新花 $0.0090 超过上限 $0.01」自相矛盾。付费跑里给的是
                #   $11 / $229.77 这种数，看不出问题，但报告不许印与事实不符的数。
                print(f"\n[熔断] 本轮新花 ${rep.new_spend_usd:.4f} 超过上限 "
                      f"${max_spend:g}，停止派发新批。\n"
                      "        已完成的成果照常落盘（xlsx / 清单 / 报告）。\n"
                      "        续跑：重跑同一条命令（缓存命中不花钱）并调高 --max-spend。",
                      flush=True)
            # ---- 429 熔断。位置与理由和花费熔断完全相同：判断必须发生在
            #      **抢到派发槽之后**，放 worker 开头等于不存在。
            #      为什么需要：llm.py 没有自适应降速，退避期间还占着并发槽，
            #      而全量口径的并发从没在付费口验过。智谱那次 26/44 批 429
            #      整轮作废 —— 那种局面下继续派发只是把钱和时间一起烧掉。
            #      阈值：绝对量 >= 20（避开冷启动抖动）**且** 超过已完成调用的 10%。
            _n429 = client.stats.get("err_429", 0)
            if (not a.allow_429 and not stop_flag["hit"]
                    and _rate_limit_tripped(_n429, rep.calls)):
                stop_flag["hit"] = True
                stop_flag["why"] = "429 熔断"
                rep.stop_reason = "429 熔断"
                print(f"\n[熔断] 429 已 {_n429} 次（已完成调用 {rep.calls}），"
                      f"停止派发新批。\n"
                      f"        并发 {a.concurrency} 很可能超了服务商上限 ——"
                      f"有实测背书的值是 40。\n"
                      "        已完成的成果照常落盘；续跑：**降低 --concurrency**"
                      "后重跑同一条命令\n"
                      "        （已完成的批走缓存不重复花钱）。确认要顶着限流跑"
                      "就加 --allow-429。",
                      flush=True)
            # ---- 余额不足熔断。**第一次命中就停** —— 这是它和 429 的关键差别：
            #      429 是瞬时的，退避后可能就好了；余额不足是**终局**的，
            #      一旦出现，后面每一批都会同样失败。
            #      不停会怎样（实测口径）：余额在第 3,000 批耗尽，剩下 2,852 批
            #      全部被派发一遍、每批一次快速失败（402/403 不在重试名单里，
            #      不退避、不烧钱），几十秒后报出 call_failures ≈ 2852 ——
            #      不白花钱，但那 2,852 条失败全是噪声，而且**你以为跑完了，
            #      其实是撞墙了**。
            #      好消息：失败的批不落缓存，所以充值后重跑同一条命令会
            #      **精确重试这些批，花的钱只剩那些批**。
            if (not a.allow_no_balance and not stop_flag["hit"]
                    and client.stats.get("err_balance", 0) >= 1):
                stop_flag["hit"] = True
                stop_flag["why"] = "余额不足"
                rep.stop_reason = "余额不足"
                print("\n[熔断] 账户余额不足，停止派发新批。\n"
                      "        已完成的成果照常落盘（xlsx / 清单 / 报告）。\n"
                      "        ⚠ 失败的批**没有**写进缓存，所以充值后重跑同一条"
                      "命令即可\n"
                      "          精确续传 —— 已完成的批走缓存不重复花钱，"
                      "只花剩下那些批。\n"
                      "          同一个 --out 记得加 --force（过陈旧 xlsx 闸门）。\n"
                      "        确认要继续（比如只是想跑完看结构）就加"
                      " --allow-no-balance。",
                      flush=True)
            if done % 20 == 0 or done == len(tasks):
                print(f"  进度 {done}/{len(tasks)}  术语 {rep.terms_kept}"
                      f"  失败 {rep.call_failures}"
                      f"  本轮新花 ${rep.new_spend_usd:.4f}", flush=True)

    # return_exceptions=True 是第二道保险：兜住 worker 自己万一出的意外，
    # 保证 gather 一定返回、导出一定发生。
    _res = await asyncio.gather(
        *(worker(f, i, b) for f, i, b in tasks), return_exceptions=True)
    for _r in _res:
        if isinstance(_r, BaseException) and not isinstance(_r, asyncio.CancelledError):
            rep.batch_exceptions += 1
            rep.errors.append(f"gather 兜底: {type(_r).__name__}: {_r}"[:500])
    await client.aclose()

    # ⚠ 这些计数以前只写不打印，正是「把限流当成质量差」那个坑的来源。
    rep.rate_limited = client.stats.get("err_429", 0)
    rep.balance_errors = client.stats.get("err_balance", 0)
    if client.stats:
        print("\n调用层计数:", dict(sorted(client.stats.items())))

    # 分层的文件计数
    for f in all_sel:
        rep.per_layer[f.layer()]["files"] += 1

    # ---- 导出：md5 组内每个成员都出一份 xlsx（§2 行 14）
    # 交付前统一词典形：同一文件里同一中文术语 + 同一切片只能有一个词典形，
    # 否则交付物自相矛盾（`selfcheck` 的「结构性问题」档就是查这个）。$0，不重跑。
    # 行序按句序。⚠ 以前没有这一步：并发 worker 直接 extend、导出不排序，
    # 所以行序 = **批完成顺序**（实测第 2 批整块排在第 1 批前面）。下游工具方的 HTML 工具
    # 是串行的，她的产出永远按句序；我们 1,620 个 xlsx 行序随机、每次跑还不一样。
    # 稳定排序，只按 sent_id —— 同一句内术语的先后是模型给的语义顺序，不动。
    def _sid_key(r):
        v = getattr(r, "sent_id", None)
        if isinstance(v, bool):
            return (2, 0, "")
        if isinstance(v, int):
            return (0, v, "")
        sv = str(v if v is not None else "")
        return (1, 0, sv) if not sv.isdigit() else (0, int(sv), "")

    for _rows in rows_by_md5.values():
        _rows.sort(key=_sid_key)

    # 交付值标点归一化（外语侧外层引号 + 撇号字符）。放在 unify 之前 ——
    # 先把同一个词的写法统一，再做多数票，否则 `Yan’an` 与 `Yan'an` 会被当两个值。
    normed = 0
    for _rows in rows_by_md5.values():
        normed += normalize_delivery(_rows)
    if normed:
        print(f"交付值标点归一化：{normed} 处（外语侧外层引号 / 撇号字符）")
    rep.delivery_normalized = normed

    unified = 0
    for _rows in rows_by_md5.values():
        unified += unify_dict_forms(_rows)
    if unified:
        print(f"词典形一致性收口：统一了 {unified} 处（同文件同切片多词典形）")
    rep.dict_form_unified = unified

    written: list[Path] = []
    for f in all_sel:
        rows = rows_by_md5.get(f.md5, [])
        rep.per_file[f.basename] = len(rows)
        # ⚠ **交付后**的「词典形 != 句中切片」行数，单独数一遍。
        #   上游的 `nom_changed` 是**收口前**的数：`normalize_delivery`（标点归一化）
        #   与 `unify_dict_forms`（同文件多数票）都在它之后才动交付值，所以两个数
        #   **本来就不相等**（西语实测 14,669 vs 14,780）。报告不印这个数，
        #   下游拿 `nom_changed` 去核 xlsx 就会以为少了 111 行。
        for r in rows:
            if (r.out_src or "") != (r.term_src or ""):
                rep.rows_dict_changed += 1
            if (r.out_tgt or "") != (r.term_tgt or ""):
                rep.rows_dict_changed += 1
        if not rows:
            rep.files_zero_terms += 1
            continue
        p = out_dir / f.out_name
        if write_xlsx(rows, p, DEFAULT_EXPORT):
            written.append(p)
    rep.files_written = len(written)
    print(f"\n写出 xlsx {len(written)} 个 -> {out_dir}")

    # ---- 机读原始行：句中切片与词条形式两套都留着，term_qc 环节要靠它溯源。
    #      以前完全不落盘，跑完全量后原句切片只剩在响应缓存里、没法核对。
    #
    # ⚠ **字段名与交付 xlsx 的列名不是一一对应，别按字面套**（2026-09-19 记）：
    #     本文件 `term_src` / `term_tgt`        = 词条形式  <-> xlsx 第 8/9 列 `term_*_dict`
    #     本文件 `term_src_span` / `_tgt_span`  = 句中切片  <-> xlsx 第 4/5 列 `term_src` / `term_tgt`
    #     本文件 `term_*_base`                  = 模型给的原始词典形（未回落）
    #   为什么名字不一致：`term_src` 一直是「词条形式」的意思，而 `qc_workbook` /
    #   `selfcheck` / `probe` 三个模块按这些名字读它 —— 为了对齐列名去改名，会**静默**
    #   打断它们（字段读不到就静默走回落分支，一个字都不报）。所以在这里做显式映射，
    #   由 `verify.load_span_index` 负责核对两边同源。
    if not a.no_raw_dump:
        raw = []
        for f in all_sel:
            for r in rows_by_md5.get(f.md5, []):
                raw.append({
                    "file": f.basename, "out_name": f.out_name,
                    "layer": f.layer(), "sent_id": r.sent_id,
                    "src_text": r.src_text, "tgt_text": r.tgt_text,
                    "term_src": r.out_src, "term_tgt": r.out_tgt,
                    "term_src_span": r.term_src, "term_tgt_span": r.term_tgt,
                    "term_src_base": r.term_src_base,
                    "term_tgt_base": r.term_tgt_base,
                    "types": r.types,
                })
        rawp = out_dir / "terms_raw.json"
        rawp.parent.mkdir(parents=True, exist_ok=True)
        rawp.write_text(json.dumps(
            {"lang": a.lang, "model": a.model, "effort": a.effort,
             "prompt": prompt, "phash": phash, "rows": raw},
            ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"原始行落盘: {rawp}（{len(raw)} 行）")

    # ---- 失败批闸门：交付包不能在「运行不完整」时静默产出。
    #      失败批的句子在 xlsx 里表现为「这句没有术语」，与真的没术语**无法区分**，
    #      而以前导出这一步没有任何失败率判断。
    package_ok = True
    if rep.incomplete_batches:
        lst = out_dir / "未完成_失败批清单.txt"
        lst.parent.mkdir(parents=True, exist_ok=True)
        rate = rep.incomplete_rate
        lst.write_text(
            f"本轮 {rep.batches} 批中有 {rep.incomplete_batches} 批不完整"
            f"（{rate:.2%}）：调用层失败 {rep.call_failures}"
            f"（其中本地异常 {rep.batch_exceptions}）、"
            f"拒答/空响应 {rep.refused_batches}、"
            f"纠正重试后仍解析不出 JSON {rep.json_dead_batches}、"
            f"花费熔断跳过 {rep.skipped_by_budget}。\n"
            "这些批的句子在 xlsx 里表现为「没有术语」，与真的没术语无法区分。\n"
            "⚠ 拒答与截断的响应**不写缓存**，所以重跑同一条命令会重试它们；\n"
            "   调用层失败同理。几乎不额外花钱。\n\n"
            + (f"⚠ 429 共 {rep.rate_limited} 次 —— 并发可能超了服务商上限。\n"
               if rep.rate_limited else "")
            + "\n== 可定位清单（文件 + 批号 + 句号区间）==\n"
            + "\n".join(rep.dead_batches)
            # ⚠ 封顶 500 以前是**静默截断**，清单看着是完整的。
            + (f"\n（另有 {rep.dead_batches_total - len(rep.dead_batches)} 条"
               f"未列出：清单封顶 500）"
               if rep.dead_batches_total > len(rep.dead_batches) else "")
            + ("\n\n== 熔断未派发的批 ==\n" + "\n".join(rep.skipped_batches)
               if rep.skipped_batches else "")
            + "\n\n== 调用层错误 ==\n"
            + "\n".join(rep.errors), encoding="utf-8")
        print(f"\n⚠ {rep.incomplete_batches}/{rep.batches} 批不完整（{rate:.2%}）"
              f"：失败 {rep.call_failures} / 拒答 {rep.refused_batches}"
              f" / JSON 死 {rep.json_dead_batches}"
              f" / 熔断跳过 {rep.skipped_by_budget}，清单: {lst}")
        if rate > INCOMPLETE_GATE:
            package_ok = a.allow_incomplete

            if not package_ok:
                print(f"[闸门] 不完整批比例超过 {INCOMPLETE_GATE:.2%}，"
                      "**不产出 results.zip 与 README.md**。\n"
                      "        交给下游工具方的包必须是完整的。请重跑同一条命令补齐失败批\n"
                      "        （缓存续跑，只花失败那几批的钱），"
                      "或确认后加 --allow-incomplete。\n"
                      "        xlsx 已经写好，不受影响。")
    else:
        # ⚠ **本轮没有失败批时，必须把上一轮留下的那份清单删掉。**
        #   它是按「本轮」写的，不删就会一直躺在目录里冒充「本轮」。
        #   2026-09-19 真栽过：一轮因余额熔断中断留下「1849 批不完整（31.6%）」，
        #   之后的续跑全部成功（不完整批 0），而那个文件**既不重写也不删** ——
        #   一名审计员照它得出了「语料三成没跑、两个域是残的」的**错误结论**，
        #   而我们花了一整轮才证伪。它不在 results.zip 里，所以不影响交付内容；
        #   但它会误导任何看这个目录的人 —— 包括我们自己。
        _stale_lst = out_dir / "未完成_失败批清单.txt"
        if _stale_lst.exists():
            _stale_lst.unlink()
            print(f"（已删除上一轮遗留的 {_stale_lst.name} —— 本轮不完整批为 0）")

    # 字段说明 markdown：**随交付包一起走**。交付物就是这份 zip，说明不跟着数据走，
    # 对方解压出来就只有一堆表 —— 他那边（和他的 AI）读的就是这份。
    # ⚠ 必须在 pack_zip **之前**写，否则打到的是上一轮的旧文件。
    rep.finished = datetime.now().isoformat(timespec="seconds")
    field_spec = None
    if written and package_ok:
        field_spec = write_field_spec(out_dir, rep.finished)

    # ⚠ 给下游工具方的那份说明**也必须随包走**（2026-09-19 审计发现，之前不在 zip 里）。
    #   它解释「为什么 12 个文件的 sent_id 与上游不一致、这批要按位置关联」，
    #   以及全部上游异常。它不在包里 = 对方拿到的是「承诺 + 数据」，
    #   而解释留在我们机器上 —— 这正是会被退回来的那类。
    #   字段说明里那句 sent_id 例外的提示，就是指向这份文件。
    delivery_note = ROOT / DELIVERY_NOTE_NAME
    if not delivery_note.exists():
        delivery_note = None

    if written and not a.no_zip and package_ok:
        zp = pack_zip(written, out_dir, DEFAULT_EXPORT,
                      extra=tuple(x for x in (field_spec, delivery_note) if x))
        print(f"打包: {zp}")

    # ⚠ **语料层做过的修复必须出现在报告里**，不能只躺在 `CorpusFile.notes` 里。
    #   法语是重灾区：字段错位还原 **11 个文件 / 1,528 句**、JSON 定点修复 **6 个文件**、
    #   文件名拼写纠正 **7 个** —— 这三项此前**只在报告里印不出来**，
    #   非法语语种全是 0，所以从没暴露。修复本身是好的（`tests_fr.py` 钉着），
    #   但**读报告的人看不到「这轮救回过 1,528 句」**。审计口径：
    #   归因要读证据，报告要印发生过的事。
    def _n_files(kw: str) -> int:
        return sum(1 for f in all_sel if any(kw in n for n in f.notes))

    rep.corpus_issues = {
        "跳过的非 JSON 条目": len(skipped),
        "md5 去重省下的调用": f"{len(all_sel) - len(reps)} 个文件",
        "触发 sent_id 重编的文件": _n_files("重编"),
        "异种 schema 文件": sum(1 for f in all_sel if f.schema != "standard"),
        # 下面三项是**修复**（把本来会丢/会错的内容救回来），不是「问题」。
        # 印出来是为了让报告能回答「这一轮语料被动过什么手脚」。
        "字段错位已还原的文件": (
            f"{_n_files('建议复查')} 个，共 "
            f"{sum(f.shifted_recovered for f in all_sel)} 句"),
        "JSON 语法定点修复的文件": _n_files("定点修复"),
        "文件名拼写已纠正的文件": _n_files("拼写已纠正"),
    }

    txt, js = rep.save(out_dir, tag)
    print()
    print(rep.to_text())
    print(f"\n报告: {txt}\n      {js}")

    if written and package_ok:
        write_readme(
            out_dir, lang=a.lang, ts=rep.finished, model=a.model,
            effort=a.effort or "(默认)", batch_size=a.batch_size,
            prompt=prompt, phash=phash,
            columns=", ".join(DEFAULT_EXPORT.columns),
            sheet=DEFAULT_EXPORT.sheet_name,
            lang_specific=DICT_FORM_CALIBER if a.lang in DICT_FORM_LANGS else "",
            issues="\n".join(f"- {k}: {v}" for k, v in rep.corpus_issues.items()),
            # ⚠ 报 `rows_written`（= xlsx 实际数据行总和）而不是 `terms_kept`。
            #   两者在有「字节完全相同的重复输入文件」时会差出那几份的行数
            #   （西语实测差 156）—— 对方数一遍 xlsx 就能发现，报错数会被追问。
            stats=f"- 文件 {rep.files_written} 个，术语 {rep.rows_written} 行"
                  f"（= 表里实际的数据行）\n"
                  f"- 逐字校验通过率 {rep.anchor_pass_rate:.2%}\n"
                  f"- JSON 解析成功率 {rep.json_ok_rate:.2%}"
                  + (f"\n- 词典形还原成功 {rep.nom_ok} 条，回落 {rep.nom_fallback} 条"
                     f"（{rep.nom_fallback_rate:.2%}）" if rep.nom_total else ""),
        )
    # ⚠ 以前这里无条件 return 0：交付包被闸门拦下（没有 zip、没有 README）的一轮，
    #   在脚本里、在我自己的复述里，都是「跑完了」。闸门只打印不改退出码 ==
    #   闸门在自动化里不存在。
    if not package_ok:
        print("\n[退出码 3] 交付包**没有产出** —— results.zip 与 README.md 被闸门"
              "拦下了。\n"
              "        xlsx 已经写好不受影响，但这一轮**不算成功**。")
        return 3
    return 0


def main(argv=None) -> int:
    a = build_args(argv)
    try:
        return asyncio.run(main_async(a))
    except KeyboardInterrupt:
        print("\n已中断。缓存已落盘，重跑同样命令可断点续跑。")
        return 130


if __name__ == "__main__":
    # ⚠ 必须在 **任何打印之前**调（包括 `--help` 与 argparse 的报错）。
    #   另外 11 个入口都有这一行，只有本文件漏了 —— 而它恰恰是打印中文最多的那个：
    #   Windows 控制台默认 GBK，打西语 `ñ`、法语 `ç`、俄语西里尔或 `⚠` 会抛
    #   UnicodeEncodeError，**报告已经算完了却整份丢掉**（config.utf8_stdout 的
    #   docstring 记着 2026-09-18 的实测）。一次五小时的付费运行撞上就是白跑。
    utf8_stdout()
    sys.exit(main())
