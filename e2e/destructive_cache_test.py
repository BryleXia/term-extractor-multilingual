# -*- coding: utf-8 -*-
r"""破坏性测试：确认「解析死批的缓存真的被撤掉、而合法空数组的缓存真的留着」。

为什么必须真跑而不能只看断言：熔断第一版的教训是**「断言过了、实测无效」**。
这一轮改的是缓存的**副作用**（文件在不在磁盘上），源码断言完全证明不了。

**全程零 API 调用**：预先把缓存文件种好，`LLMClient.call()` 会在读缓存那一步就返回
（`llm.py` 命中即返回，在信号量之前），并且用 `correct_retry=False` 避免纠正重试打出去。
跑完会核对 `client.stats` 里没有 `calls`。

⚠ **缓存根指向 tempfile 临时目录**，全程不碰真 `cache/`。这是 `tests.py:405` 早就在用的
惯例（`cl.cache_root = Path(_td)`）。本脚本第一版没复用它，而是在真 `cache/` 下建测试
目录、收尾 `rmtree(cache_root.parent)` —— 而 `cache_root` 本身已经是
`cache/<模型__档位>/<phash>`，它的 parent 是**该模型共享的整个缓存目录**。
那一次真的把 `cache/gemini-3.7-flash__high/` 全删了（1,408 个已缓存批次）。
定稿 dump 在 `bakeoff/` 所以交付侧无损，但那些批再也不能零成本重放。
教训有两条：① 现成的安全惯例要先找再写；② destructive 清理的目标必须是自己建的路径。
"""
import asyncio
import json
import os
import pathlib
import sys
import tempfile
from dataclasses import asdict

# 本脚本从 `e2e/` 子目录运行，先把仓库根加进 sys.path（原来它在根目录下）。
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# ⚠ 本脚本**从不发真实请求**（假 client 或假 HTTP 层），但 `LLMClient.__init__`
#   会读一次 key 才肯构造。给个哑值即可 —— 环境变量优先于 key 文件，真 key 碰不到。
#   这样一来这个脚本**任何人拿到都能跑**，不需要先配一把真 key。
os.environ.setdefault("INFERERA_API_KEY", "sk-dummy-e2e-never-sends-a-request")
# 公开仓库里没有生产语料 —— 用 fixtures/ 里的**合成夹具**（内容全部虚构）。
from getterms import config as _cfg
_cfg.CORPUS_DIRS["es"] = (pathlib.Path(__file__).resolve().parent.parent
                          / "fixtures" / "corpus_es")


from getterms.corpus import Sentence
from getterms.extract import batch_key, build_messages, run_batch
from getterms.llm import CallResult, LLMClient

MD5 = "f" * 32
IDX = 9001
FAILED = []


def ck(name, ok, detail=""):
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f"   ({detail})" if detail else ""))
    if not ok:
        FAILED.append(name)


BATCH = [Sentence(pos=1, out_id=1, src="景德镇的瓷器很有名。",
                  tgt="La porcelana de Jingdezhen es famosa."),
         Sentence(pos=2, out_id=2, src="这是第二句。", tgt="Esta es la segunda.")]
BKEY = batch_key(BATCH)
SYS, TPL = "system", "{pairs}"
GOOD = json.dumps([{"sent_id": 1, "terms": [
    {"term_src": "景德镇", "term_tgt": "Jingdezhen", "types": "cul"}]}],
    ensure_ascii=False)


def seed(client, idx, content, wire_fp=None):
    """把一条「调用成功」的缓存种到（临时的）缓存根里。"""
    r = CallResult(ok=True, content=content, finish_reason="stop",
                   prompt_tokens=10, completion_tokens=10,
                   model=client.price_key, effort=client.effort,
                   wire_fp=client.wire_fp if wire_fp is None else wire_fp)
    p = client._cache_path(MD5, idx, BKEY)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(asdict(r), ensure_ascii=False), encoding="utf-8")
    return p


with tempfile.TemporaryDirectory() as _td:
    client = LLMClient("gemini-3.7-flash", "high", "z" * 12, concurrency=1)
    client.cache_root = pathlib.Path(_td)      # ← 全程不碰真 cache/
    print(f"缓存根（临时）: {client.cache_root}")
    print(f"wire 指纹: {client.wire_fp}\n")
    try:
        # ---- 1) 散文响应：缓存必须被撤掉
        print("[1] 解析不出来的响应（散文）-> 缓存应被撤掉")
        p1 = seed(client, IDX, "抱歉，这一批我无法按要求输出 JSON。")
        ck("种缓存成功", p1.exists())
        oc, calls = asyncio.run(run_batch(client, SYS, TPL, BATCH, MD5, IDX,
                                          correct_retry=False, lang="es"))
        ck("命中了缓存（没有真发调用）", bool(calls) and calls[0].from_cache)
        ck("判为 parse_failed", oc.parse_failed, f"json_failed={oc.json_failed}")
        ck("缓存文件已被撤掉", not p1.exists(), "这是「重跑能补齐」的前提")
        ck("撤缓存被计数", client.stats.get("cache_invalidated", 0) >= 1,
           f"cache_invalidated={client.stats.get('cache_invalidated', 0)}")

        # ---- 2) 合法空数组：缓存必须留着
        print("\n[2] 模型给合法空数组 `[]` -> 缓存应保留（那是最终答案）")
        p2 = seed(client, IDX + 1, "[]")
        oc2, calls2 = asyncio.run(run_batch(client, SYS, TPL, BATCH, MD5, IDX + 1,
                                            correct_retry=False, lang="es"))
        ck("命中了缓存", bool(calls2) and calls2[0].from_cache)
        ck("json_failed 为真（这批确实没术语，进闸门）", oc2.json_failed)
        ck("parse_failed 为假", not oc2.parse_failed)
        ck("缓存文件仍在", p2.exists(),
           "撤了就等于每次重跑为「确实没术语」重新付费")

        # ---- 3) 正常响应：缓存留着、术语进来
        print("\n[3] 正常响应 -> 缓存保留且术语入表")
        p3 = seed(client, IDX + 2, GOOD)
        oc3, _ = asyncio.run(run_batch(client, SYS, TPL, BATCH, MD5, IDX + 2,
                                       correct_retry=False, lang="es"))
        ck("缓存文件仍在", p3.exists())
        ck("术语被保留", oc3.n_kept >= 1, f"n_kept={oc3.n_kept}")
        ck("不是 parse_failed", not oc3.parse_failed)

        # ---- 4) wire 参数变了 -> 当未命中
        print("\n[4] wire 参数指纹不一致 -> 当未命中（不能拿旧参数的结果冒充新参数）")
        seed(client, IDX + 3, GOOD, wire_fp="deadbeef0000")
        ck("读缓存判为未命中", client._read_cache(MD5, IDX + 3, BKEY) is None)
        ck("计数 cache_param_mismatch",
           client.stats.get("cache_param_mismatch", 0) >= 1,
           f"{client.stats.get('cache_param_mismatch', 0)}")

        # ---- 5) 旧缓存（没有指纹字段）-> 照旧接受
        print("\n[5] 旧缓存没有 wire_fp 字段 -> 照旧接受（不制造一次全量重跑）")
        p5 = seed(client, IDX + 4, GOOD, wire_fp="")
        d = json.loads(p5.read_text(encoding="utf-8"))
        d.pop("wire_fp", None)          # 模拟本轮改动之前落的盘
        p5.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        got5 = client._read_cache(MD5, IDX + 4, BKEY)
        ck("旧缓存仍然命中", got5 is not None and got5.from_cache)
        ck("计数 cache_no_fp", client.stats.get("cache_no_fp", 0) >= 1,
           f"{client.stats.get('cache_no_fp', 0)}")

        # ---- 6) 全程零 API 调用
        print("\n[6] 全程零 API 调用")
        ck("client.stats 里没有 calls", "calls" not in client.stats,
           f"stats={dict(sorted(client.stats.items()))}")
    finally:
        asyncio.run(client.aclose())

print()
if FAILED:
    print(f"破坏性测试失败 {len(FAILED)} 项：")
    for x in FAILED:
        print(f"  - {x}")
    sys.exit(1)
print("破坏性测试全部通过（零 API 调用，未触碰真 cache/）")
