"""针对审计中发现的两处与计划不符之处做定点核查（一次性诊断脚本）。

1. 88 处「有内容但无字母」的垃圾单元集中在哪些文件；
2. 计划里标为「跳号」的 5 个文件，实际是跳号还是 id 重复。
"""
from __future__ import annotations

import collections

from .corpus import load_corpus, is_meaningful

TARGETS = [
    "zh-es_tour_muse_0023_seg002",
    "zh-es_tour_muse_0024_seg002",
    "es-zh_conf_poli_0036_seg001",
    "es-zh_conf_poli_0007_seg001",
    "zh-es_conf_poli_0023_seg001",
]


def main() -> None:
    files, _ = load_corpus()

    print("=== 1. 垃圾单元（有内容但无字母/汉字）按文件汇总 ===")
    per_file = collections.Counter()
    samples: dict[str, list[str]] = {}
    for f in files:
        for s in f.sentences:
            for side, txt in (("src", s.src), ("tgt", s.tgt)):
                if txt and not is_meaningful(txt):
                    per_file[f.basename] += 1
                    samples.setdefault(f.basename, []).append(f"第{s.pos}句{side}={txt!r}")
    total = sum(per_file.values())
    print(f"合计 {total} 处，分布在 {len(per_file)} 个文件")
    for name, cnt in per_file.most_common():
        print(f"  {cnt:3d}  {name}")
        for sm in samples[name][:4]:
            print(f"         {sm}")

    print("\n=== 2. 计划标为「跳号」的 5 个文件的真实 id 形态 ===")
    by_stem = {f.stem_key: f for f in files}
    for t in TARGETS:
        f = by_stem.get(t)
        if f is None:
            print(f"  {t}: 未找到")
            continue
        ids = [s.out_id for s in f.sentences]
        n = len(ids)
        renumbered = any("重编" in x for x in f.notes)
        print(f"\n  {t}")
        print(f"    句数={n}  已重编={renumbered}")
        if renumbered:
            print(f"    原因: {[x for x in f.notes if '重编' in x][0]}")
        else:
            gaps = sorted(set(range(1, max(ids) + 1)) - set(ids))
            print(f"    沿用原 id；最大={max(ids)}；缺号={gaps}")

    print("\n=== 3. 直接看这 5 个文件的原始 sent_id（前 12 个 + 重复统计）===")
    import json
    import zipfile
    from pathlib import Path
    from .config import CORPUS_DIR
    from .corpus import _decode_member, _find_sentence_list, _pick, _ID_KEYS

    want = {t: None for t in TARGETS}
    for zp in sorted(CORPUS_DIR.glob("*.zip")):
        with zipfile.ZipFile(zp) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                m = _decode_member(info)
                base = Path(m).name
                for t in TARGETS:
                    if base.startswith(t) and base.endswith(".json"):
                        want[t] = json.loads(z.read(info).decode("utf-8-sig"))
    for t, data in want.items():
        if data is None:
            continue
        items, _ = _find_sentence_list(data)
        raw = [_pick(i, _ID_KEYS) for i in items]
        dup = [k for k, v in collections.Counter(raw).items() if v > 1]
        print(f"\n  {t}: n={len(raw)}  前12={raw[:12]}")
        print(f"    重复的 id: {sorted(dup)[:20]}{' ...' if len(dup) > 20 else ''}  共 {len(dup)} 个值重复")
        print(f"    含 0: {0 in raw}   最大: {max(x for x in raw if isinstance(x, int))}")


if __name__ == "__main__":
    main()
