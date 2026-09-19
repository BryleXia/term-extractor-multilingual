"""语料审计：跑一遍 corpus.py，复核计划里那张异常清单。

用法：python -m getterms.audit --lang fr
不调 API、不花钱。**每个新语种到手先跑这个**，不预设与已做过的语种同构
—— 法语实测确实多出 4 类西语没有的缺陷。
"""
from __future__ import annotations

import argparse
import collections
import json
import sys

from .config import OUT_DIR, corpus_dir
from .corpus import (
    cjk_ratio, dedup_by_md5, is_meaningful, load_corpus, zip_overlaps,
)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="getterms.audit", description="语料模式审计")
    ap.add_argument("--lang", default="es")
    a = ap.parse_args(argv)
    cdir = corpus_dir(a.lang)
    if not cdir.exists():
        print(f"语料目录不存在: {cdir}")
        return 1

    # --- zip 之间的重叠与包含关系（法语实测有 57 个跨 zip 重名）
    print(f"语料目录: {cdir}")
    zips = sorted(cdir.glob("*.zip"))
    print(f"zip 个数: {len(zips)}")
    ov = zip_overlaps(cdir)
    print(f"\n[zip 重叠] 有交集的 zip 对: {len(ov)}")
    for x, y, n, rel in ov:
        print(f"    {x} x {y}   交集 {n} 个   {rel}")

    files, skipped = load_corpus(cdir)
    print(f"\n去重后唯一文件: {len(files)}    跳过/提示: {len(skipped)}")
    for s in skipped:
        print(f"    [跳过] {s}")

    # --- 新增 A：JSON 语法修复（法语 6 个）
    repaired = [f for f in files if any("定点修复" in n for n in f.notes)]
    fallback = [f for f in files if any("退回正则" in n for n in f.notes)]
    print(f"\n[新增 A] JSON 语法有错、已定点修复的文件: {len(repaired)}")
    for f in repaired:
        why = [n for n in f.notes if "定点修复" in n][0]
        print(f"    {f.basename}  句数={len(f.sentences)}  {why}")
    if fallback:
        print(f"    ⚠ 修不好只能退回正则的: {len(fallback)}"
              f" -> {[f.basename for f in fallback]}")

    # --- 新增 B：字段整体错位还原（法语 11 个 / 1528 句）
    shifted = [f for f in files if f.shifted_recovered]
    print(f"\n[新增 B] 字段整体错位已还原的文件: {len(shifted)}"
          f"，共 {sum(f.shifted_recovered for f in shifted)} 句")
    for f in sorted(shifted, key=lambda x: -x.shifted_recovered):
        print(f"    {f.basename}  还原 {f.shifted_recovered}/{len(f.sentences)} 句"
              f"  ← 上游对齐质量可疑，建议复查")
    unrec = [f for f in files if any("无法还原" in n for n in f.notes)]
    if unrec:
        print(f"    ⚠ 错位且还原不了的文件: {[f.basename for f in unrec]}")

    # --- 新增 C：文件名拼写纠正（与 HTML 的第三处有意差异）
    renamed = [f for f in files if any("拼写已纠正" in n for n in f.notes)]
    print(f"\n[新增 C] 文件名拼写已纠正: {len(renamed)}（这是与 HTML 的有意差异，"
          f"映射表要给王敬）")
    for f in renamed:
        print(f"    {f.basename}  ->  {f.out_name}")

    # --- 行 2/3：命名变体
    naming = collections.Counter()
    dot_named = []
    for f in files:
        if f.basename.endswith("_align.qc.json"):
            naming["_align.qc.json"] += 1
        elif f.basename.endswith(".align.qc.json"):
            naming[".align.qc.json"] += 1
            dot_named.append((f.basename, f.out_name))
        else:
            naming["其他"] += 1
    print("\n[行 2/3] 命名变体:", dict(naming))
    for b, o in dot_named:
        print(f"    点号命名: {b}  ->  {o}")

    # --- 行 5/6/7：schema 变体
    schemas = collections.Counter(f.schema for f in files)
    print("\n[行 5/6/7] schema 分布:", dict(schemas))
    for f in files:
        if f.schema not in ("standard",):
            print(f"    异种: {f.basename}  schema={f.schema}  句数={len(f.sentences)}")

    # --- 行 8/9：sent_id 重编
    renumbered = [f for f in files if any("重编" in n for n in f.notes)]
    print(f"\n[行 8/9] 触发 sent_id 重编的文件: {len(renumbered)}")
    for f in renumbered:
        why = [n for n in f.notes if "重编" in n][0]
        print(f"    {f.basename}  句数={len(f.sentences)}  {why}")

    # 跳号但仍沿用原 id 的（无害）
    gapped = []
    for f in files:
        if f in renumbered or not f.sentences:
            continue
        ids = [s.out_id for s in f.sentences]
        if ids != list(range(1, len(ids) + 1)):
            gapped.append((f.basename, len(ids), max(ids)))
    print(f"    跳号但沿用原 id（无害）: {len(gapped)}")
    for b, n, mx in gapped:
        print(f"        {b}  句数={n}  最大 id={mx}")

    # --- 行 10/11：空文本
    empty_cells = 0
    files_with_empty = 0
    nonstr_hits = []
    for f in files:
        e = 0
        for s in f.sentences:
            if not is_meaningful(s.src):
                e += 1
            if not is_meaningful(s.tgt):
                e += 1
            for side, txt in (("src", s.src), ("tgt", s.tgt)):
                if txt and not is_meaningful(txt):
                    nonstr_hits.append(f"{f.basename} 第{s.pos}句 {side}={txt!r}")
        if e:
            files_with_empty += 1
        empty_cells += e
    print(f"\n[行 10] 空文本单元: {empty_cells} 处，分布在 {files_with_empty} 个文件")
    print(f"[行 11] 有内容但无字母/汉字（纯数字等）: {len(nonstr_hits)} 处")
    for h in nonstr_hits[:10]:
        print(f"    {h}")

    # --- 行 12：pair 与文件名不符
    pair_mismatch = []
    for f in files:
        pair = (f.meta.get("pair") or "").strip()
        name_dir = f.name_direction()
        if pair != name_dir:
            pair_mismatch.append((f.basename, pair or "(空)", name_dir))
    print(f"\n[行 12] pair 字段与文件名前缀不符: {len(pair_mismatch)}")
    for b, p, n in pair_mismatch:
        print(f"    {b}  pair={p}  文件名={n}")

    # --- 行 13：内容方向与文件名相反
    dir_mismatch = []
    for f in files:
        if not f.usable_sentences:
            continue
        nd = f.name_direction()
        cd = f.content_direction()
        expect = "zh-first" if nd.startswith("zh-") else "foreign-first"
        if cd != expect and cd != "unclear":
            dir_mismatch.append((f.basename, nd, cd))
    print(f"\n[行 13] 内容方向与文件名前缀相反: {len(dir_mismatch)}")
    for b, nd, cd in dir_mismatch:
        print(f"    {b}  文件名={nd}  实际={cd}")

    # --- 行 14：字节重复
    groups = dedup_by_md5(files)
    dups = {k: v for k, v in groups.items() if len(v) > 1}
    print(f"\n[行 14] 内容完全相同的组: {len(dups)}（唯一内容 {len(groups)} 份）")
    for k, v in dups.items():
        print(f"    md5={k[:8]}  {len(v)} 个: {', '.join(x.basename for x in v)}")

    # --- 行 16：0 句文件
    zero = [f for f in files if not f.sentences]
    print(f"\n[行 16] 0 句文件: {len(zero)}")
    for f in zero:
        print(f"    {f.basename}  notes={f.notes}")

    # --- 规模与分层
    total_sent = sum(len(f.sentences) for f in files)
    usable_sent = sum(len(f.usable_sentences) for f in files)
    print(f"\n=== 规模 ===")
    print(f"总句对: {total_sent}    可送模型: {usable_sent}    "
          f"不可用: {total_sent - usable_sent}")
    uniq_usable = sum(len(v[0].usable_sentences) for v in groups.values())
    print(f"去重后需实际调用的句对: {uniq_usable}（省下 {usable_sent - uniq_usable} 句）")

    layers = collections.Counter(f.layer() for f in files)
    print("\n分层（scene/domain -> 文件数）:")
    for k, v in sorted(layers.items(), key=lambda x: -x[1]):
        sc = sum(len(f.usable_sentences) for f in files if f.layer() == k)
        print(f"    {k:16s} 文件 {v:4d}   可用句对 {sc:6d}")

    dirs = collections.Counter(f.name_direction() for f in files)
    print("\n文件名方向:", dict(dirs))

    # 落盘一份机读审计结果
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    audit_path = OUT_DIR / f"corpus_audit_{a.lang}.json"
    audit_path.write_text(json.dumps({
        "lang": a.lang,
        "corpus_dir": str(cdir),
        "zips": [z.name for z in zips],
        "zip_overlaps": [{"a": x, "b": y, "n": n, "rel": r} for x, y, n, r in ov],
        "json_repaired": {f.basename: [n for n in f.notes if "定点修复" in n]
                          for f in repaired},
        "shifted_recovered": {f.basename: f.shifted_recovered for f in shifted},
        "renamed": {f.basename: f.out_name for f in renamed},
        "entries": len(files),
        "skipped": skipped,
        "naming": dict(naming),
        "schemas": dict(schemas),
        "renumbered": [f.basename for f in renumbered],
        "gapped": [b for b, _, _ in gapped],
        "empty_cells": empty_cells,
        "files_with_empty": files_with_empty,
        "pair_mismatch": [b for b, _, _ in pair_mismatch],
        "direction_mismatch": [b for b, _, _ in dir_mismatch],
        "dup_groups": {k[:8]: [x.basename for x in v] for k, v in dups.items()},
        "total_sentences": total_sent,
        "usable_sentences": usable_sent,
        "unique_usable_sentences": uniq_usable,
        "layers": {k: v for k, v in layers.items()},
        "name_directions": dict(dirs),
        "per_file_notes": {f.basename: f.notes for f in files if f.notes},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n机读审计结果已写入: {audit_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
