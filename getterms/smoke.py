"""推理档位接洽验收（计划 §5 冒烟必测清单）。

回答 7 个问题，每个都用实测数据回答，不靠文档推断：
  1. reasoning_effort 是否真的透传 -> 看 reasoning_tokens 随档位变化
  2. response_format=json_object 是否被接受
  3. usage 里有没有 cached_tokens（决定提示词缓存省钱能否兑现）
  4. finish_reason 取值分布、空响应/拒答
  5. 实际延迟（决定并发数）
  6. 免费变体与付费变体行为是否一致
  7. max_completion_tokens / max_tokens 是否把思考 token 算在内

用法：
  python -m getterms.smoke                # 只跑免费变体，不花钱
  python -m getterms.smoke --paid         # 跑全部付费模型（约 $0.1~0.5）
  python -m getterms.smoke --paid --models glm-5.3
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .config import OUT_DIR, key_fingerprint, load_api_key
from .corpus import load_corpus
from .extract import build_messages, load_prompt, parse_json_array, validate_batch
from .llm import LLMClient
from .profiles import PROFILES, get as get_profile

FREE_MODELS = ["gemini-3.7-flash-free", "gemini-3.8-flash-free"]
PAID_MODELS = ["gemini-3.7-flash", "gemini-3.8-flash", "glm-5.3",
               "qwen3.8-max-2026-09-02"]


def pick_batch(n: int = 5):
    """固定取一个 conf/poli 文件的前 n 句 —— 时政语料，同时测拒答风险。"""
    files, _ = load_corpus()
    cands = [f for f in files if f.layer() == "conf/poli" and len(f.usable_sentences) >= n]
    f = sorted(cands, key=lambda x: x.basename)[0]
    return f, f.usable_sentences[:n]


async def one_call(model: str, effort: str | None, system: str, ut: str,
                   batch, f, *, cache: bool, override: dict | None = None,
                   idx: int = 0) -> dict:
    client = LLMClient(model, effort, "smoke", concurrency=1, use_cache=cache,
                       param_override=override)
    kwargs = client.wire_params()
    msgs = build_messages(system, ut, batch)
    r = await client.call(msgs, f.md5, idx)
    await client.aclose()
    items = parse_json_array(r.content)
    oc = validate_batch(items, batch)
    return {
        "model": model, "effort": effort, "sent_params": kwargs,
        "ok": r.ok, "error": r.error,
        "finish_reason": r.finish_reason,
        "prompt_tokens": r.prompt_tokens, "cached_tokens": r.cached_tokens,
        "completion_tokens": r.completion_tokens,
        "reasoning_tokens": r.reasoning_tokens,
        "visible_tokens": r.completion_tokens - r.reasoning_tokens,
        "latency_s": r.latency_s, "attempts": r.attempts,
        "content_len": len(r.content),
        "json_parsed": bool(items), "terms_proposed": oc.n_items,
        "terms_kept": oc.n_kept, "refused": r.refused,
        "cost": round(r.cost, 6),
    }


def fmt(rows: list[dict]) -> str:
    L = []
    a = L.append
    a(f"{'模型':26s} {'档位':8s} {'finish':10s} {'in':>7s} {'cached':>7s} "
      f"{'out':>7s} {'思考':>7s} {'可见':>7s} {'延迟':>7s} {'术语':>5s} {'成本$':>9s}")
    a("-" * 116)
    for r in rows:
        if not r["ok"]:
            a(f"{r['model']:26s} {str(r['effort'] or '-'):8s} 失败: {r['error'][:60]}")
            continue
        a(f"{r['model']:26s} {str(r['effort'] or '默认'):8s} "
          f"{str(r['finish_reason'] or '-'):10s} "
          f"{r['prompt_tokens']:7d} {r['cached_tokens']:7d} "
          f"{r['completion_tokens']:7d} {r['reasoning_tokens']:7d} "
          f"{r['visible_tokens']:7d} {r['latency_s']:6.1f}s "
          f"{r['terms_kept']:5d} {r['cost']:9.5f}")
    return "\n".join(L)


async def main_async(a) -> int:
    key = load_api_key()
    print(f"API key: {key_fingerprint(key)}")
    system, ut, _ = load_prompt(a.prompt)
    f, batch = pick_batch(a.sentences)
    print(f"测试样本: {f.basename}  {f.layer()}  前 {len(batch)} 句（时政语料，兼测拒答）\n")

    models = a.models.split(",") if a.models else (
        FREE_MODELS + PAID_MODELS if a.paid else FREE_MODELS)

    # ---- 测试 1/2/3/4/5：逐模型逐档位
    print("=" * 116)
    print("测试 1-5：档位透传 / json_object / cached_tokens / finish_reason / 延迟")
    print("=" * 116)
    rows: list[dict] = []
    for m in models:
        prof = get_profile(m)
        for i, eff in enumerate(prof.effort_enum):
            r = await one_call(m, eff, system, ut, batch, f,
                               cache=not a.no_cache, idx=i)
            rows.append(r)
            print(fmt([r]).splitlines()[-1], flush=True)
    print()
    print(fmt(rows))

    # ---- 判定 1：档位是否真透传
    print("\n" + "=" * 116)
    print("判定 1：reasoning_effort 是否真的透传（思考 token 应随档位单调上升）")
    print("=" * 116)
    verdicts = {}
    for m in models:
        rs = [r for r in rows if r["model"] == m and r["ok"]]
        if len(rs) < 2:
            continue
        thinks = [(r["effort"], r["reasoning_tokens"]) for r in rs]
        distinct = len({t for _, t in thinks}) > 1
        rising = all(thinks[i][1] <= thinks[i + 1][1] for i in range(len(thinks) - 1))
        verdicts[m] = {"thinks": thinks, "distinct": distinct, "monotonic": rising}
        mark = "透传OK" if distinct else "⚠ 各档思考 token 相同，档位可能未生效"
        print(f"  {m:26s} {thinks}  -> {mark}"
              f"{'  (单调上升)' if distinct and rising else ''}")

    # ---- 判定 3：缓存
    print("\n" + "=" * 116)
    print("判定 3：提示词缓存 —— 同一请求打第二次，cached_tokens 是否 > 0")
    print("=" * 116)
    cache_rows = []
    for m in models:
        prof = get_profile(m)
        eff = prof.effort_enum[0]
        # 关掉本地缓存，强制真发两次
        r1 = await one_call(m, eff, system, ut, batch, f, cache=False, idx=900)
        r2 = await one_call(m, eff, system, ut, batch, f, cache=False, idx=901)
        cache_rows.append((m, r1["cached_tokens"], r2["cached_tokens"],
                           r1["prompt_tokens"]))
        print(f"  {m:26s} 第1次 cached={r1['cached_tokens']:6d}  "
              f"第2次 cached={r2['cached_tokens']:6d}  (输入 {r1['prompt_tokens']})"
              f"  -> {'缓存生效' if r2['cached_tokens'] > 0 else '未见缓存命中'}")

    # ---- 判定 7：输出上限是否含思考 token
    print("\n" + "=" * 116)
    print("判定 7：max_completion_tokens / max_tokens 是否把思考 token 算在内")
    print("   做法：给最高档配一个很小的上限(2000)，看是截断可见输出还是连思考都装不下")
    print("=" * 116)
    for m in models:
        prof = get_profile(m)
        top = prof.effort_enum[-1]
        r = await one_call(m, top, system, ut, batch, f, cache=False,
                           override={prof.max_out_param: 2000}, idx=902)
        if not r["ok"]:
            print(f"  {m:26s} {top:6s} 失败: {r['error'][:70]}")
            continue
        concl = ("上限含思考 token（思考已占满、可见输出被挤掉）"
                 if r["visible_tokens"] <= 0 or (r["finish_reason"] == "length"
                                                 and r["reasoning_tokens"] > 0)
                 else "上限似只管可见输出")
        print(f"  {m:26s} {top:6s} finish={r['finish_reason']:8s} "
              f"思考={r['reasoning_tokens']:5d} 可见={r['visible_tokens']:5d} "
              f"术语={r['terms_kept']:3d}  -> {concl}")

    total = sum(r["cost"] for r in rows)
    print(f"\n本次冒烟总成本（含缓存与上限测试之外的主表）: ${total:.4f}")

    out = OUT_DIR / "smoke_matrix.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"rows": rows, "tier_verdicts": verdicts,
         "cache": [{"model": m, "first": a1, "second": b1, "prompt_tokens": p}
                   for m, a1, b1, p in cache_rows]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"机读结果: {out}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="getterms.smoke")
    p.add_argument("--paid", action="store_true", help="包含付费模型")
    p.add_argument("--models", default=None, help="逗号分隔，覆盖默认清单")
    p.add_argument("--prompt", default="es_v1")
    p.add_argument("--sentences", type=int, default=5)
    p.add_argument("--no-cache", action="store_true")
    a = p.parse_args(argv)
    return asyncio.run(main_async(a))


if __name__ == "__main__":
    sys.exit(main())
