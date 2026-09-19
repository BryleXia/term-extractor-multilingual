"""官方直连对照探针：验证中转站是否忠实转发推理档位。

为什么要做：中转站实测发现两个反常现象——
  * Qwen3.8-Max 的 low/medium/xhigh 三档思考 token 几乎一样（4182~4843），档位形同无效；
  * GLM-5.3 的 low 档思考 6257 token / 68 秒，比 high(393) 贵 16 倍。
这可能是中转站没转发参数，也可能是模型本身如此。用官方直连打同样的请求就能分辨。

**用完全相同的 5 句 + 同一份提示词**，才能与 cache/*/smoke/ 里的中转站数据直接对比。

用法：
  python -m getterms.direct_probe                    # 全部
  python -m getterms.direct_probe --provider zhipu   # 只打一家
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from openai import AsyncOpenAI

from .config import OUT_DIR, ROOT, key_fingerprint
from .extract import build_messages, load_prompt, parse_json_array, validate_batch
from .smoke import pick_batch

KEY_DIR = ROOT / "手上有的" / "api-keys"


@dataclass(frozen=True)
class Direct:
    name: str
    key_file: str
    base_url: str
    models: tuple[str, ...]
    tiers: tuple[str, ...]
    # 有些厂商要求把档位放 extra_body
    effort_in_extra_body: bool = False
    send_response_format: bool = True


PROVIDERS = {
    "bailian": Direct(
        name="bailian",
        key_file="bailian.txt",
        # 官方 OpenAI 兼容端点（qwen.ai 博客与 help.aliyun.com 一致）
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        # ⚠ 命名差异：中转站叫 qwen3.8-max-2026-09-02，百炼叫 qwen3.8-max-0902。
        #   实拉百炼 /models（252 个）确认：只有 qwen3.8-max 与 qwen3.8-max-0902。
        #   百炼上同时有第三方的 glm-5.3 / glm-5.2 / deepseek-v4-pro 等。
        models=("qwen3.8-max-0902", "qwen3.8-max"),
        tiers=("low", "medium", "xhigh"),   # 官方博客注明：supported levels are xhigh, medium, and low
    ),
    "zhipu": Direct(
        name="zhipu",
        key_file="glm.txt",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        models=("glm-5.3",),
        tiers=("low", "high", "max"),
    ),
    "deepseek": Direct(
        name="deepseek",
        key_file="deepseek.txt",
        base_url="https://api.deepseek.com/v1",
        models=(),        # 不在候选集，只列一下在架模型
        tiers=(),
    ),
}


def read_key(fn: str) -> str:
    p = KEY_DIR / fn
    if not p.exists():
        raise FileNotFoundError(f"缺少 key 文件 {p}")
    k = p.read_text(encoding="utf-8-sig").strip()
    if not k:
        raise ValueError(f"{p} 为空")
    return k


async def list_models(d: Direct) -> list[str]:
    c = AsyncOpenAI(api_key=read_key(d.key_file), base_url=d.base_url, timeout=30)
    try:
        r = await c.models.list()
        return sorted(m.id for m in r.data)
    finally:
        await c.close()


async def probe_one(d: Direct, model: str, tier: str, system: str, ut: str,
                    batch) -> dict:
    c = AsyncOpenAI(api_key=read_key(d.key_file), base_url=d.base_url,
                    timeout=300, max_retries=0)
    kwargs: dict = {}
    extra: dict = {}
    if tier:
        if d.effort_in_extra_body:
            extra["reasoning_effort"] = tier
        else:
            kwargs["reasoning_effort"] = tier
    if d.send_response_format:
        kwargs["response_format"] = {"type": "json_object"}
    if extra:
        kwargs["extra_body"] = extra

    out = {"provider": d.name, "model": model, "tier": tier, "params": {
        k: v for k, v in kwargs.items() if k != "extra_body"} | (
        {"extra_body": extra} if extra else {})}
    t0 = time.monotonic()
    try:
        msgs = build_messages(system, ut, batch)
        r = await c.chat.completions.create(model=model, messages=msgs, **kwargs)
        dt = time.monotonic() - t0
        ch = r.choices[0] if r.choices else None
        content = (getattr(getattr(ch, "message", None), "content", None) or "") if ch else ""
        u = getattr(r, "usage", None)
        pt = getattr(u, "prompt_tokens", 0) or 0
        ct = getattr(u, "completion_tokens", 0) or 0
        pd = getattr(u, "prompt_tokens_details", None)
        cd = getattr(u, "completion_tokens_details", None)
        cached = (getattr(pd, "cached_tokens", 0) or 0) if pd else 0
        think = (getattr(cd, "reasoning_tokens", 0) or 0) if cd else 0
        items = parse_json_array(content)
        oc = validate_batch(items, batch)
        out.update(ok=True, finish=getattr(ch, "finish_reason", None),
                   prompt_tokens=pt, cached_tokens=cached,
                   completion_tokens=ct, reasoning_tokens=think,
                   visible=ct - think, latency_s=round(dt, 1),
                   terms_kept=oc.n_kept, json_ok=bool(items))
    except Exception as e:  # noqa: BLE001
        out.update(ok=False, error=f"{type(e).__name__}: {e}"[:300],
                   latency_s=round(time.monotonic() - t0, 1))
    finally:
        await c.close()
    return out


# 中转站（inferera/aihubmix）上同样 5 句 + 同一提示词的实测值，用于并排对比
GATEWAY_BASELINE = {
    ("qwen3.8-max-2026-09-02", "low"):    (4455, 833, 128.8),
    ("qwen3.8-max-2026-09-02", "medium"): (4843, 731, 138.6),
    ("qwen3.8-max-2026-09-02", "xhigh"):  (4182, 1032, 118.8),
    ("glm-5.3", "low"):  (6257, 706, 68.0),
    ("glm-5.3", "high"): (393, 739, 9.9),
    ("glm-5.3", "max"):  (708, 670, 11.9),
}


async def main_async(a) -> int:
    system, ut, phash = load_prompt(a.prompt)
    f, batch = pick_batch(a.sentences)
    print(f"提示词 {a.prompt} (hash {phash})")
    print(f"样本 {f.basename} {f.layer()} 前 {len(batch)} 句 —— 与中转站冒烟完全同一批\n")

    names = [a.provider] if a.provider else list(PROVIDERS)
    results: list[dict] = []

    for n in names:
        d = PROVIDERS[n]
        try:
            k = read_key(d.key_file)
        except (FileNotFoundError, ValueError) as e:
            print(f"[{n}] 跳过：{e}")
            continue
        print(f"=== {n} ===  key {key_fingerprint(k)}  base={d.base_url}")
        try:
            ms = await list_models(d)
            print(f"  在架模型 {len(ms)} 个"
                  + (f"，含我们要的: {[m for m in ms if m in d.models]}" if d.models else ""))
            if not d.models:
                print(f"  清单前 12: {ms[:12]}")
        except Exception as e:  # noqa: BLE001
            print(f"  models 列举失败: {type(e).__name__}: {e}"[:200])
            ms = []

        for model in d.models:
            if ms and model not in ms:
                print(f"  [{model}] 不在该账号的在架清单里，跳过")
                continue
            for tier in d.tiers:
                r = await probe_one(d, model, tier, system, ut, batch)
                results.append(r)
                if r["ok"]:
                    base = GATEWAY_BASELINE.get((model, tier))
                    cmp = ""
                    if base:
                        bt, bv, bl = base
                        cmp = (f"   [中转站 思考={bt} 可见={bv} {bl}s]"
                               f"  思考差 {r['reasoning_tokens'] - bt:+d}")
                    print(f"  {model:26s} {tier:7s} finish={str(r['finish']):8s} "
                          f"in={r['prompt_tokens']:5d} cached={r['cached_tokens']:5d} "
                          f"思考={r['reasoning_tokens']:6d} 可见={r['visible']:5d} "
                          f"{r['latency_s']:6.1f}s 术语={r['terms_kept']:3d}{cmp}")
                else:
                    print(f"  {model:26s} {tier:7s} 失败: {r['error'][:110]}")
            # 只用第一个可用的模型 id 打全档位，够回答问题了
            if any(x["ok"] and x["model"] == model for x in results):
                break
        print()

    # ---- 裁决
    print("=" * 100)
    print("裁决：档位行为是模型自身的，还是中转站转发不忠实")
    print("  判据是**单调性**（思考 token 应随档位上升），不是离散度 ——")
    print("  离散度会被异常值撑大，反而把坏结果读成好结果。")
    print("=" * 100)

    def monotonic(pairs: list[tuple[str, int]]) -> bool:
        v = [x for _, x in pairs]
        return all(v[i] <= v[i + 1] for i in range(len(v) - 1))

    for n in names:
        rs = [r for r in results if r["provider"] == n and r["ok"]]
        if len(rs) < 2:
            continue
        model = rs[0]["model"]
        order = {t: i for i, t in enumerate(PROVIDERS[n].tiers)}
        rs.sort(key=lambda r: order.get(r["tier"], 99))
        direct = [(r["tier"], r["reasoning_tokens"]) for r in rs]
        gw = [(t, GATEWAY_BASELINE[(model, t)][0])
              for t, _ in direct if (model, t) in GATEWAY_BASELINE]
        d_mono = monotonic(direct)
        g_mono = monotonic(gw) if len(gw) == len(direct) else None
        print(f"  {n} / {model}")
        print(f"    直连  {direct}   单调={'是' if d_mono else '否'}")
        if g_mono is None:
            print("    中转  （无同档位基线，无法对比）")
            continue
        print(f"    中转  {gw}   单调={'是' if g_mono else '否'}")
        if d_mono and not g_mono:
            bad = [t for (t, dv), (_, gv) in zip(direct, gw) if gv > dv * 3 + 200]
            print(f"    -> **中转站转发不忠实**：直连单调、中转不单调，"
                  f"异常档位 {bad or '见上'}。这些档位不要走中转站。")
        elif d_mono and g_mono:
            print("    -> 两条路都单调，档位行为一致；没有迁移的必要。")
        elif not d_mono and not g_mono:
            print("    -> 直连也不单调 -> 是模型自身对该任务不区分档位，"
                  "不是中转站的问题。换直连不会改善。")
        else:
            print("    -> 中转站反而更规矩，没有迁移理由。")
        # 延迟对比（决定吞吐）
        lat_d = {r["tier"]: r["latency_s"] for r in rs}
        lat_g = {t: GATEWAY_BASELINE[(model, t)][2]
                 for t, _ in direct if (model, t) in GATEWAY_BASELINE}
        print(f"    延迟  直连 {lat_d}   中转 {lat_g}")

    out = OUT_DIR / "direct_probe.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"prompt": a.prompt, "prompt_hash": phash, "sample": f.basename,
         "sentences": len(batch), "gateway_baseline": {
             f"{k[0]}|{k[1]}": {"reasoning": v[0], "visible": v[1], "latency_s": v[2]}
             for k, v in GATEWAY_BASELINE.items()},
         "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n机读结果: {out}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="getterms.direct_probe")
    p.add_argument("--provider", default=None, choices=sorted(PROVIDERS))
    p.add_argument("--prompt", default="es_v1",
                   help="默认 es_v1，与中转站冒烟基线一致，便于对比")
    p.add_argument("--sentences", type=int, default=5)
    a = p.parse_args(argv)
    return asyncio.run(main_async(a))


if __name__ == "__main__":
    sys.exit(main())
