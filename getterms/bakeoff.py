"""西语 bake-off（计划 §4.4）：同一份样本、六个参赛组合、预注册判据。

样本 = 6 个 scene/domain 层 × 每层 70 句（原方案 600 句的 70%）= ≤ 420 句对，
两个方向都覆盖。**样本对所有参赛者完全相同**，否则没有可比性。

第一步比模型（批大小固定 10）：
  python -m getterms.bakeoff --step1
第二步在胜者上定批大小：
  python -m getterms.bakeoff --step2 --model gemini-3.7-flash --effort medium
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

from .config import BAKEOFF_DIR, corpus_dir, key_fingerprint, load_api_key, cost_usd, utf8_stdout
from .corpus import CorpusFile, Sentence, dedup_by_md5, load_corpus
from .extract import (DICT_FORM_LANGS, load_lemmatizer, load_morph,
                      load_prompt, run_batch)
from .llm import LLMClient
from .profiles import BAKEOFF_ENTRIES, get as get_profile
from .report import RunReport

# 每个语种的层不一样 —— 法语多一个 tour/scen（80 个文件、13,798 句、占 21%，
# 是口语化旅拍 vlog，术语密度与会议语料差别很大，必须单独占一份配额）。
LAYERS_BY_LANG = {
    "es": ["tour/muse", "tour/attr", "tour/serv", "conf/poli", "conf/econ", "conf/tech"],
    "fr": ["tour/muse", "tour/attr", "tour/serv", "tour/scen",
           "conf/poli", "conf/econ", "conf/tech"],
    # 俄语全量 2026-09-18 到手，audit 复核层名：与西语同构的 6 层（无 tour/scen）。
    # ⚠ 已按上面那条自己留的警告重跑过 audit，不是沿用测试语料的 4 层。
    #   这件事有实际后果：ru_v5 的小样出自测试语料，只覆盖 4 层，
    #   tour/attr(71 文件) + tour/muse(63 文件) ≈ 全量 25% **从未被测过**，
    #   要单独打探针并附给俄语老师。
    "ru": ["tour/muse", "tour/attr", "tour/serv", "conf/poli", "conf/econ", "conf/tech"],
    # 原来那 10 个散装测试文件只有这 4 层。保留它是因为 ru_v5 的小样出自这里，
    # 断言要能继续钉住那份小样的出处。每层只有 2~3 个文件，
    # 默认 --per-file 10 凑不满 70 句，要用 --per-file 35。
    "ru_test": ["tour/serv", "conf/poli", "conf/econ", "conf/tech"],
}
LAYERS = LAYERS_BY_LANG["es"]        # 向后兼容
PER_LAYER = 70          # = 原方案 100 句/层 的 70%
PER_FILE = 10           # 每个文件只取一小段，避免"一个层=一个讲者"


def select_sample(per_layer: int = PER_LAYER,
                  per_file: int = PER_FILE,
                  lang: str = "es") -> list[tuple[CorpusFile, list[Sentence]]]:
    """确定性分层取样：每层 per_layer 句，摊到多个文件上，方向尽量各半。

    为什么摊开取：一个文件 = 一段录音 = 一个讲者一个话题。若整层 70 句都来自
    同一文件，比出来的是"谁更懂这一个话题"，不是"谁更会抽术语"。每文件取
    per_file 句（默认 10 = 一个批次），一层就覆盖 7 个文件。

    为什么从文件 1/4 处开始取：开头往往是寒暄和自我介绍，术语密度不代表全局。

    实测数据形态（getterms.audit）：es-zh 方向只存在于 conf/poli 与 tour/serv
    两层，其余四层语料本身全是 zh-es —— 所以"两个方向都覆盖"只在这两层可达。

    确定性来自：文件按 basename 排序、切片位置由长度决定。同参数永得同样本。
    """
    files, _ = load_corpus(corpus_dir(lang))
    reps = {m: v[0] for m, v in dedup_by_md5(files).items()}
    pool = sorted(reps.values(), key=lambda f: f.basename)
    layers = LAYERS_BY_LANG.get(lang) or sorted({f.layer() for f in pool})

    out: list[tuple[CorpusFile, list[Sentence]]] = []
    for layer in layers:
        in_layer = [f for f in pool if f.layer() == layer]
        by_dir: dict[str, list[CorpusFile]] = {}
        for f in in_layer:
            by_dir.setdefault(f.name_direction(), []).append(f)
        dirs = sorted(by_dir)
        # 有几个方向就把额度均分，某方向不够时余额由其他方向补
        quota = {d: per_layer // len(dirs) for d in dirs}
        for i, d in enumerate(dirs[: per_layer % len(dirs)]):
            quota[d] += 1

        taken = 0
        leftover_files: list[CorpusFile] = []
        for d in dirs:
            need = quota[d]
            for f in by_dir[d]:
                if need <= 0:
                    leftover_files.append(f)
                    continue
                us = f.usable_sentences
                start = len(us) // 4
                chunk = us[start:start + min(per_file, need)]
                if not chunk:
                    chunk = us[:min(per_file, need)]
                if chunk:
                    out.append((f, chunk))
                    need -= len(chunk)
                    taken += len(chunk)
            # 该方向文件用尽仍不足，额度让给后面的方向
            if need > 0 and d != dirs[-1]:
                quota[dirs[-1]] += need

        # 全部方向都取过一轮还差，就在剩余文件里继续摊
        short = per_layer - taken
        for f in leftover_files:
            if short <= 0:
                break
            us = f.usable_sentences
            start = len(us) // 4
            chunk = us[start:start + min(per_file, short)]
            if chunk:
                out.append((f, chunk))
                short -= len(chunk)
    return out


def sample_summary(sample) -> dict:
    d: dict = {}
    for f, sents in sample:
        k = f.layer()
        e = d.setdefault(k, {"files": set(), "sent": 0, "dirs": {}})
        e["files"].add(f.basename)
        e["sent"] += len(sents)
        # 方向键按语料实际出现的写（zh-es/es-zh/zh-fr/fr-zh…），不硬编码语种
        e["dirs"][f.name_direction()] = e["dirs"].get(f.name_direction(), 0) + len(sents)
    return {k: {"files": len(v["files"]), "sent": v["sent"], **v["dirs"]}
            for k, v in sorted(d.items())}


async def run_entry(model: str, effort: str | None, sample, batch_size: int,
                    concurrency: int, prompt: str, dump_dir: Path | None,
                    sample_tag: str = "", lang: str = "es",
                    rep_idx: int = 1) -> RunReport:
    """跑一臂。`rep_idx` 是重复采样的轮次，从 1 起。

    ⚠ 轮次靠**缓存命名空间**区分（phash 后面缀 `__r{n}`），不是靠 `--no-cache`：
      第 1 轮沿用原目录（已花的钱不重花），第 2 轮起是独立新样本且可断点续跑。
      报告里的 `phash` 仍写真实提示词哈希，别把轮次污染进版本身份。
    """
    system, ut, phash = load_prompt(prompt)
    rep = RunReport(model=model, effort=effort, prompt=prompt, phash=phash,
                    batch_size=batch_size, concurrency=concurrency, lang=lang)
    cache_ns = phash if rep_idx <= 1 else f"{phash}__r{rep_idx}"
    client = LLMClient(model, effort, cache_ns, concurrency=concurrency)

    tasks = []
    for f, sents in sample:
        batches = [sents[i:i + batch_size] for i in range(0, len(sents), batch_size)]
        for i, b in enumerate(batches):
            tasks.append((f, i, b))
    rep.batches = len(tasks)
    rep.sentences_sent = sum(len(b) for _, _, b in tasks)
    rep.files_called = len({f.basename for f, _, _ in tasks})
    # ⚠ 以前 bake-off 从不设这个 -> 恒为 None -> report.gate_verdict() 里
    #   `if self.morph_enabled is False` 那条淘汰**对 bake-off 永不生效**。
    #   而 bake-off 正是决定采用哪一版提示词的地方：在没装形态库的环境里跑，
    #   会拿到一份「闸门全过」而词典形根本没校验过的报告。
    #   （参数名是 lang，不是 a.lang。）
    rep.morph_enabled = (
        (load_morph() if lang == "ru" else load_lemmatizer()) is not None
        if lang in DICT_FORM_LANGS else None)
    rep.files_total = rep.files_called

    all_rows: list = []
    done = 0

    async def one(f, idx, batch):
        nonlocal done
        oc, calls = await run_batch(client, system, ut, batch, f.md5, idx, lang=lang)
        for c in calls:
            if c.from_cache:
                rep.cache_hits += 1
            else:
                rep.calls += 1
                if not c.ok:
                    rep.call_failures += 1
                    if c.error:
                        rep.errors.append(f"{f.basename} b{idx}: {c.error}")
            # ⚠ 与 run.py 同一套口径：只有真的发出去的调用才算本轮新花的钱。
            #   漏了这一句会让 bake-off 报「本轮新花 $0」，而钱其实花了。
            if not c.from_cache:
                rep.new_cost_usd += cost_usd(rep.model, c.prompt_tokens,
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
        rep.terms_proposed += oc.n_items
        rep.terms_kept += oc.n_kept
        rep.bad_anchor_src += oc.bad_anchor_src
        rep.bad_anchor_tgt += oc.bad_anchor_tgt
        rep.bad_types += oc.bad_types
        rep.bad_sent_id += oc.bad_sent_id
        rep.nom_ok += oc.nom_ok
        rep.nom_fallback += oc.nom_fallback
        rep.nom_changed += oc.nom_changed
        rep.nom_plural_kept += oc.nom_plural_kept
        rep.nom_reject_same += oc.nom_reject_same
        # ⚠ 这两个以前漏了 -> 指标在 bake-off 报告里结构性恒为 0。
        #   实测三份定稿报告都写 nom_comp_changed=0，而重算法语是 1
        #   （`aliments à base de farines` -> `aliment à base de farine`，
        #    正是法语老师 ② 要的结果）。不当闸门，但必须能看见。
        rep.nom_comp_changed += oc.nom_comp_changed
        rep.nom_comp_notes.extend(f"{f.basename} {x}" for x in oc.nom_comp_notes)
        rep.nom_failures.extend(f"{f.basename} {x}" for x in oc.nom_failures)
        d = rep.per_layer[f.layer()]
        d["sent"] += len(batch)
        d["terms"] += oc.n_kept
        for r in oc.rows:
            all_rows.append({"file": f.basename, "layer": f.layer(),
                             "sent_id": r.sent_id, "src": r.src_text, "tgt": r.tgt_text,
                             # 交付值（俄语=词典形，其余=原句切片）与切片两套都留，
                             # 小样与 QC 工作簿要并排展示「词典形 / 句中形式」。
                             "term_src": r.out_src, "term_tgt": r.out_tgt,
                             "term_src_span": r.term_src, "term_tgt_span": r.term_tgt,
                             "term_src_base": r.term_src_base,
                             "term_tgt_base": r.term_tgt_base,
                             "types": r.types})
        done += 1
        if done % 15 == 0 or done == len(tasks):
            print(f"    {model} {effort or '默认'}: {done}/{len(tasks)}"
                  f"  术语 {rep.terms_kept}  ${rep.cost:.4f}", flush=True)

    await asyncio.gather(*(one(f, i, b) for f, i, b in tasks))
    await client.aclose()
    for f, _ in sample:
        pass
    for layer in rep.per_layer:
        rep.per_layer[layer]["files"] = len(
            {f.basename for f, _ in sample if f.layer() == layer})
    rep.finished = datetime.now().isoformat(timespec="seconds")

    if dump_dir is not None:
        dump_dir.mkdir(parents=True, exist_ok=True)
        safe = model.replace('.', '_').replace('/', '__')
        tagn = f"{safe}__{effort or 'default'}__b{batch_size}{sample_tag}"
        (dump_dir / f"terms_{tagn}.json").write_text(
            json.dumps(all_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return rep


def compare_table(reps: list[RunReport], target_sentences: int | None = None,
                  lang: str = "es") -> str:
    """并排对比表。

    ⚠ 外推分母必须按语种取（`report.FULL_CORPUS`）。先前这里写死 56,110（西语），
    于是法语那一栏按小 16% 的分母外推、**成本被系统性低估**，俄语更是拿一个
    未交付的全量去外推。全量未交付的语种（`FULL_CORPUS` 里句数为 None）
    改印「$/千句」。
    """
    from .report import FULL_CORPUS
    lang_name, full_n = FULL_CORPUS.get(lang, ("西语", 56110))
    if target_sentences is None:
        target_sentences = full_n
    L = []
    a = L.append
    a("=" * 132)
    a(f"{lang_name} bake-off 并排对比（判据在跑之前已写死，见计划 §4.4）")
    a("=" * 132)
    hdr = (f"{'模型':24s} {'档位':7s} {'批':>3s} {'JSON':>7s} {'锚定':>7s} "
           f"{'密度':>6s} {'拒答':>5s} {'截断':>5s} {'思考%':>7s} "
           f"{'延迟':>7s} {'本轮$':>8s} "
           + (f"{'外推全量$':>10s} " if target_sentences else f"{'$/千句':>10s} ")
           + "裁决")
    a(hdr)
    a("-" * 132)
    for r in reps:
        think_pct = (r.reasoning_tokens / r.completion_tokens
                     if r.completion_tokens else 0)
        verdict = "; ".join(r.gate_verdict())
        a(f"{r.model:24s} {str(r.effort or '默认'):7s} {r.batch_size:3d} "
          f"{r.json_ok_rate:6.1%} {r.anchor_pass_rate:6.1%} "
          f"{r.terms_per_sentence:6.2f} {r.refused:5d} {r.truncated:5d} "
          f"{think_pct:6.1%} "
          f"{(r.latency_sum / r.calls if r.calls else 0):6.1f}s "
          f"{r.cost:8.4f} "
          + (f"{r.extrapolate(target_sentences):10.2f}" if target_sentences
             else f"{r.cost / r.sentences_sent * 1000:10.3f}" if r.sentences_sent
             else f"{'-':>10s}")
          + f"  {verdict}")
    a("")
    a("分层术语密度（个/句）—— tour 与 conf 要分开看")
    layers = sorted({l for r in reps for l in r.per_layer})
    a(f"{'模型/档位':32s} " + " ".join(f"{l:>11s}" for l in layers))
    for r in reps:
        cells = []
        for l in layers:
            d = r.per_layer.get(l)
            cells.append(f"{(d['terms'] / d['sent'] if d and d['sent'] else 0):11.2f}")
        a(f"{r.model + '/' + str(r.effort or '默认'):32s} " + " ".join(cells))
    a("")
    a("conf/poli 拒答（一票否决项）")
    for r in reps:
        d = r.per_layer.get("conf/poli", {})
        a(f"  {r.model:24s} {str(r.effort or '默认'):7s} "
          f"句 {d.get('sent', 0):4d}  拒答 {d.get('refused', 0):3d}  "
          f"术语 {d.get('terms', 0):4d}")
    return "\n".join(L)


def _guard_label(label: str, overwrite: bool) -> None:
    """已有同名落盘结果就拦住。

    ⚠ label 是这一臂的**全部身份**（模型/档位/批大小/语种/样本配置/提示词/--tag）。
      两次实验算出同一个 label，说明有一个变量没进签名 —— 那种情况下继续跑
      **会静默覆盖旧结果**，而旧结果可能是语言老师已经批准过的基线。
      语料内容变了而目录名没变（俄语 ru 从测试语料改指全量）就属于这一类。
    """
    p = BAKEOFF_DIR / f"terms_{label}.json"
    if p.exists() and not overwrite:
        import datetime
        mt = datetime.datetime.fromtimestamp(p.stat().st_mtime)
        raise SystemExit(
            f"\n[拒绝开跑] 已存在同名结果：{p.name}"
            f"\n            落盘时间 {mt:%Y-%m-%d %H:%M}，{p.stat().st_size / 1024:.0f} KB"
            "\n  这个 label 是本臂的全部身份。同名意味着要么你在重放同一个实验，"
            "\n  要么有个实验变量没进签名（语料换了？样本换了？）。"
            "\n  确认要覆盖就加 --overwrite；想并存就用 --tag 给这次实验起个名字。")


async def main_async(a) -> int:
    utf8_stdout()          # 中文报告/中文拒绝理由必须能打出来，见 config.utf8_stdout
    print(f"API key: {key_fingerprint(load_api_key())}")
    from .run import DEFAULT_PROMPT
    if a.prompt is None:
        a.prompt = DEFAULT_PROMPT.get(a.lang, f"{a.lang}_v1")
    sample = select_sample(a.per_layer, a.per_file, lang=a.lang)
    summ = sample_summary(sample)
    total = sum(v["sent"] for v in summ.values())
    print(f"\n样本：{total} 句对（每层上限 {a.per_layer}），文件 "
          f"{len({f.basename for f, _ in sample})} 个")
    for k, v in summ.items():
        # 方向键按语料实际出现的写（zh-es/es-zh/zh-fr/fr-zh/zh-ru…），不硬编码语种
        dirs = " / ".join(f"{dk} {dv}" for dk, dv in sorted(v.items())
                          if dk not in ("sent", "files"))
        print(f"  {k:12s} 句 {v['sent']:4d}  ({dirs})  文件 {v['files']}")

    if a.dry_run:
        if a.entries:
            entries = [tuple(t.strip().rsplit(":", 1)) for t in a.entries.split(",") if t.strip()]
        else:
            entries = BAKEOFF_ENTRIES if a.step1 else [(a.model, a.effort)]
        print(f"\n[dry-run] 参赛 {len(entries)} 组，每组批次 "
              f"{sum(1 for f, s in sample for _ in range(0, len(s), a.batch_size))}")
        for m, e in entries:
            print(f"  {m}  effort={e}")
        return 0

    BAKEOFF_DIR.mkdir(parents=True, exist_ok=True)
    reps: list[RunReport] = []
    # 落盘签名必须含**全部实验变量**（语种 / 样本配置 / 提示词），否则换一个变量
    # 重跑会直接覆盖旧基线。已经踩过两次：西语那轮「每文件 30 句」盖掉了
    # 「每文件 10 句」的 gemini medium 基线（见 计划_西语_归档.md §9 bug 表 3b）；
    # 俄语这轮 ru_v1 -> ru_v2 只改提示词，文件名一模一样，差点又盖一次。
    # 默认提示词不进签名，保持已有文件名不变。
    sample_tag = "" if a.per_file == PER_FILE else f"__pf{a.per_file}"
    if a.lang != "es":
        sample_tag = f"__{a.lang}" + sample_tag
    # ⚠ 这里**故意**硬写「哪个提示词算无后缀基线」，不要改成读 run.DEFAULT_PROMPT。
    #   已经落盘的 v1 基线文件名里没有后缀；若跟着 DEFAULT_PROMPT 一起前移，
    #   新提示词的结果就会重新变成「无后缀」，直接覆盖旧基线（西语 pf30 踩过一次）。
    _default_prompt = f"{a.lang}_v3" if a.lang == "es" else f"{a.lang}_v1"
    if a.prompt != _default_prompt:
        sample_tag += f"__{a.prompt}"
    if a.tag:
        sample_tag += f"__{a.tag.strip('_')}"

    if a.repeat < 1:
        print("--repeat 至少 1")
        return 2

    if a.entries:
        entries = []
        for tok in a.entries.split(","):
            tok = tok.strip()
            if not tok:
                continue
            if ":" not in tok:
                print(f"--entries 项 {tok!r} 缺少 ':档位'")
                return 2
            m, e = tok.rsplit(":", 1)
            entries.append((m.strip(), e.strip() or None))
        print(f"\n自定义参赛 {len(entries)} 组（批大小 {a.batch_size}）"
              + (f"，每组重复采样 {a.repeat} 次" if a.repeat > 1 else "") + "\n")
        for m, e in entries:
            for k in range(1, a.repeat + 1):
                rtag = "" if k == 1 else f"__r{k}"
                _guard_label(f"{m.replace('.', '_').replace('/', '__')}__{e}"
                             f"__b{a.batch_size}{sample_tag}{rtag}", a.overwrite)
                print(f"  >>> {m}  effort={e}"
                      + (f"  第 {k}/{a.repeat} 次采样" if a.repeat > 1 else ""))
                r = await run_entry(m, e, sample, a.batch_size, a.concurrency,
                                    a.prompt, BAKEOFF_DIR, sample_tag + rtag,
                                    a.lang, rep_idx=k)
                reps.append(r)
                r.save(BAKEOFF_DIR,
                       f"{m.replace('.', '_').replace('/', '__')}__{e}"
                       f"__b{a.batch_size}{sample_tag}{rtag}")
    elif a.step1:
        entries = BAKEOFF_ENTRIES
        print(f"\n第一步 · 比模型（批大小固定 {a.batch_size}）\n")
        for m, e in entries:
            print(f"  >>> {m}  effort={e}")
            r = await run_entry(m, e, sample, a.batch_size, a.concurrency,
                                a.prompt, BAKEOFF_DIR, sample_tag, a.lang)
            reps.append(r)
            r.save(BAKEOFF_DIR,
                   f"{m.replace('.', '_')}__{e}__b{a.batch_size}{sample_tag}")
    elif a.step2:
        print(f"\n第二步 · 定批大小（模型 {a.model} effort={a.effort}）\n")
        for bs in (5, 20):
            print(f"  >>> 批大小 {bs}")
            r = await run_entry(a.model, a.effort, sample, bs, a.concurrency,
                                a.prompt, BAKEOFF_DIR, sample_tag, a.lang)
            reps.append(r)
            r.save(BAKEOFF_DIR,
                   f"{a.model.replace('.', '_')}__{a.effort}__b{bs}{sample_tag}")
    else:
        print("请指定 --step1 或 --step2")
        return 2

    table = compare_table(reps, lang=a.lang)
    print("\n" + table)
    out = BAKEOFF_DIR / ("compare_direct.txt" if a.entries
                         else "compare_step1.txt" if a.step1 else "compare_step2.txt")
    out.write_text(table, encoding="utf-8")
    print(f"\n对比表: {out}")
    print(f"样本术语明细（给语言老师看）: {BAKEOFF_DIR}/terms_*.json")
    print(f"合计花费: ${sum(r.cost for r in reps):.4f}")
    return 0


def main(argv=None) -> int:
    # ⚠ 必须在建 ArgumentParser **之前**切 UTF-8：`--help` 的中文是 argparse 自己
    #   打印的，走的是控制台默认编码（Windows 中文版是 GBK）→ UnicodeEncodeError，
    #   帮助根本看不了。原先 utf8_stdout() 只在 main_async() 里调，太晚了。
    utf8_stdout()
    p = argparse.ArgumentParser(prog="getterms.bakeoff")
    p.add_argument("--step1", action="store_true", help="比模型")
    p.add_argument("--step2", action="store_true", help="在胜者上定批大小")
    p.add_argument("--model", default=None)
    p.add_argument("--effort", default=None)
    p.add_argument("--lang", default="es", help="es / fr / ru，决定语料目录与分层")
    p.add_argument("--prompt", default=None, help="默认按语种取 <lang>_v3 / <lang>_v1")
    p.add_argument("--per-layer", type=int, default=PER_LAYER)
    p.add_argument("--per-file", type=int, default=PER_FILE,
                   help="每个文件取多少句。默认 10；测批大小时要设大于批大小，"
                        "否则批 10 与批 20 产生完全相同的分批（无效实验）")
    p.add_argument("--batch-size", type=int, default=10)
    p.add_argument("--concurrency", type=int, default=12)
    p.add_argument("--tag", default="",
                   help="把未进签名的实验变量显式写进 label（如 --tag full 表示"
                        "跑的是全量语料的样本）。语料内容换了而 label 没换 = 覆盖旧基线")
    p.add_argument("--overwrite", action="store_true",
                   help="允许覆盖已落盘的 terms_<label>.json（默认拒绝，见 _guard_label）")
    p.add_argument("--repeat", type=int, default=1,
                   help="同一臂重复采样几次（只对 --entries 生效）。"
                        "第 1 次沿用原缓存，第 2 次起用独立缓存命名空间 —— "
                        "单条术语的值在噪声里，改提示词要判 3~5%% 的差异就得 n>=2")
    p.add_argument("--entries", default=None,
                   help='自定义参赛，逗号分隔的 "模型:档位"，'
                        '如 "deepseek/deepseek-flash:low,zhipu/glm-5.3:high"')
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    return asyncio.run(main_async(a))


if __name__ == "__main__":
    sys.exit(main())
