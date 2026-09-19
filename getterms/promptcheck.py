"""提示词自检：示例本身必须遵守提示词自己定的规则。

如果示例违规，等于在教模型违规。改提示词后必须跑这个。
用法：python -m getterms.promptcheck [名字...]   （默认检查全部 prompts/*.md）
"""
from __future__ import annotations

import json
import re
import sys

from .config import utf8_stdout
from .config import ALLOWED_TYPES, PROMPT_DIR
from .corpus import cjk_ratio
from .extract import (check_lemma_romance, check_nominative, find_verbatim,
                       load_lemmatizer, load_morph, load_prompt)

FENCE = re.compile(r"```json\s*(\[[\s\S]*?\])\s*```")


def check_prompt(name: str) -> tuple[list[str], dict]:
    """返回 (问题列表, 统计)。"""
    problems: list[str] = []
    system, user_template, phash = load_prompt(name)
    info = {"name": name, "hash": phash, "chars": len(system),
            "approx_tokens": len(system) // 3, "examples": 0, "terms": 0,
            "types_covered": []}

    if "{pairs}" not in user_template:
        problems.append("user 模板缺 {pairs} 占位符")
    if "JSON" not in system.upper():
        problems.append("提示词里没有 JSON 字样 —— Qwen 的 json_object 会直接报错")

    # 提示词自己声明了词典形输出键时，示例必须逐条守自己的规则。
    # **只按提示词内容判，不按名字判** —— 没声明这个键的旧版本完全不受影响。
    # `ru_nom` 是 ru_v4 的旧键名，继续认，否则那一轮的自检会突然失败。
    key = ("dict_form" if "dict_form" in system
           else "ru_nom" if "ru_nom" in system else "")
    lang = name.split("_")[0] if "_" in name else "es"
    info["dict_key"] = key
    info["dict_lang"] = lang
    analyzer = None
    if key:
        analyzer = load_morph() if lang == "ru" else load_lemmatizer()
        info["morph"] = analyzer is not None

    blocks = FENCE.findall(system)
    if len(blocks) < 2:
        # v1 那种没有示例的基线提示词，允许
        info["examples"] = 0
        return problems, info

    try:
        inp = json.loads(blocks[0])
        outp = json.loads(blocks[1])
    except json.JSONDecodeError as e:
        problems.append(f"示例 JSON 自身无法解析: {e}")
        return problems, info

    sents = {s["sent_id"]: s for s in inp}
    info["examples"] = len(sents)

    out_ids = [el["sent_id"] for el in outp]
    if sorted(out_ids) != sorted(sents):
        problems.append(f"示例输出的 sent_id {sorted(out_ids)} 与输入 "
                        f"{sorted(sents)} 不一致（提示词要求每句都要有元素）")
    if len(set(out_ids)) != len(out_ids):
        problems.append("示例输出有重复 sent_id")

    covered: set[str] = set()
    for el in outp:
        s = sents.get(el["sent_id"])
        if s is None:
            continue
        for t in el.get("terms", []):
            info["terms"] += 1
            ts, tt = t.get("term_src", ""), t.get("term_tgt", "")
            if find_verbatim(ts, s["src"]) is None:
                problems.append(f"句{el['sent_id']} term_src {ts!r} 不是 src 的子串")
            if find_verbatim(tt, s["tgt"]) is None:
                problems.append(f"句{el['sent_id']} term_tgt {tt!r} 不是 tgt 的子串")
            types = t.get("types") or []
            if not isinstance(types, list) or not types:
                problems.append(f"句{el['sent_id']} {ts!r} 的 types 应为非空数组")
                continue
            bad = [x for x in types if x not in ALLOWED_TYPES]
            if bad:
                problems.append(f"句{el['sent_id']} {ts!r} 含非法类型 {bad}")
            if "num" in types:
                problems.append(f"句{el['sent_id']} {ts!r} 出现 num（本项目不抽 num）")
            # other 互斥：提示词自己定的规则，示例必须遵守
            if "other" in types and len(types) > 1:
                problems.append(
                    f"句{el['sent_id']} {ts!r} 把 other 与 {types} 混用，"
                    "违反 other 互斥规则")
            if t.get("note", "") != "":
                problems.append(f"句{el['sent_id']} {ts!r} 的 note 应为空串")
            if key:
                problems.extend(
                    _check_dict_form(el["sent_id"], ts, tt, t, key, lang, analyzer))
            covered.update(types)
    info["types_covered"] = sorted(covered)
    missing = sorted(set(ALLOWED_TYPES) - covered)
    if missing:
        problems.append(f"示例未覆盖类型 {missing}（覆盖不全时模型对这些类没有样例）")
    return problems, info


def _check_dict_form(sid, ts: str, tt: str, t: dict, key: str,
                    lang: str, analyzer) -> list[str]:
    """示例里的词典形必须能通过本语种的校验 —— 示例违规等于教模型违规。

    外语在哪一侧不固定，按汉字占比判：汉字少的那个术语就是外语侧。
    """
    nom = str(t.get(key) or "").strip()
    if not nom:
        return [f"句{sid} {ts!r} 缺 {key}（提示词声明了这个键，示例必须给）"]
    fo_term = ts if cjk_ratio(ts) < cjk_ratio(tt) else tt
    if lang == "ru":
        err = check_nominative(nom, fo_term, morph=analyzer)
    else:
        err = check_lemma_romance(nom, fo_term, lang, lemmatizer=analyzer)
    if err:
        return [f"句{sid} {key} {nom!r} 对外语侧 {fo_term!r} 不合格 —— {err}"]
    return []


def main(argv=None) -> int:
    utf8_stdout()
    names = argv or sys.argv[1:]
    # 这个工具的参数就是提示词名（没有 argparse），所以 -h/--help 会被当成
    # 提示词名去找 `prompts/--help.md` 然后 FileNotFoundError。显式拦一下。
    if any(n in ("-h", "--help") for n in names):
        avail = ", ".join(sorted(p.stem for p in PROMPT_DIR.glob("*.md")))
        print("用法：python -m getterms.promptcheck [提示词名 ...]")
        print("不给名字 = 检查全部。示例：python -m getterms.promptcheck es_v8 fr_v7 ru_v6")
        print(f"现有：{avail}")
        return 0
    if not names:
        names = sorted(p.stem for p in PROMPT_DIR.glob("*.md"))
    total = 0
    for n in names:
        problems, info = check_prompt(n)
        head = (f"{info['name']:8s} hash={info['hash']}  {info['chars']:5d} 字符"
                f"  ~{info['approx_tokens']:4d} token"
                f"  示例 {info['examples']} 句 / {info['terms']} 术语")
        print(head)
        if info["types_covered"]:
            print(f"         覆盖类型 {info['types_covered']}")
        if info.get("dict_key"):
            print(f"         声明了 `{info['dict_key']}` 词典形键"
                  f"（按 {info['dict_lang']} 校验）；形态分析器 "
                  + ("已启用" if info.get("morph") else "未启用（降级为关系校验）"))
        if problems:
            total += len(problems)
            for p in problems:
                print(f"         ✗ {p}")
        else:
            print("         ✓ 自检通过")
    print(f"\n合计问题 {total}")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
