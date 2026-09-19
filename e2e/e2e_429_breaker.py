# -*- coding: utf-8 -*-
r"""端到端：429 风暴时 run.py 真的会熔断吗？一个瞬时抖动会不会被**误判**成熔断？

## 为什么必须真跑一遍

429 熔断是 5.4 小时全量里**最可能真触发**的一条路径，而 2026-09-19 的审计结论是：
**全仓没有任何假 429 注入**。重试 5 次 / 退避 / 熔断 → 跳过 → 闸门 → 退出码 3
整条链只有源码字符串断言 + 纯函数单测 —— 那正是「429 熔断第一版」栽过的形状
（`run.py:265-269` 自己记着：「断言过了、实测无效」）。
唯一的真实 429 风暴（`logs/并发50实测`，43 次）跑在该熔断代码存在**之前**的版本上。

锚点只钉在 `_client.chat.completions.create` 上，所以 `LLMClient.call` 里的
**真实重试循环、退避、stats 计数、熔断判据、派发信号量**全部照跑。

## 三轮

**第一轮（一直 429）** —— 20 次尝试后必须熔断：
  退出码 3 / `stop_reason` = 429 熔断 / `rate_limited >= 20` / 后续批被跳过
  / 跳过清单可定位 / 失败的批没落缓存 / **只派发了 4 批**（停派发是真的停）。

**第二轮（`--allow-429`）** —— 逃生口必须放行：不熔断、六批全派发、跳过为 0。

**第三轮（每批只 429 一次，随后以别的可重试错误失败）** —— **反向风险**：
  `_rate_limit_tripped` 是「绝对量 >= 20 **且** 超过已完成调用的 10%」。
  一个「偶尔撞一次 429」的抖动不该触发熔断 —— 因为熔断会**连交付包一起打掉**
  （`skipped_by_budget` 计入 `incomplete_batches`）。这一轮把那个下限钉住。

**零 API 调用**（假 HTTP 层），`cache_root` 指向临时目录。
"""
import hashlib
import json
import os
import pathlib
import shutil
import sys
import tempfile

import httpx
import openai

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


import getterms.run as R
from getterms.llm import LLMClient

# ⚠ 绕过了 `run.py` 底部的 `if __name__ == "__main__": utf8_stdout()` ——
#   不显式设一次，本脚本的成败会取决于控制台编码（UTF-8 过 / GBK 挂）。
from getterms.config import utf8_stdout

utf8_stdout()

FAILED = []
MADE = []

# ⚠ 批数**不要写死**：`--limit-sentences` 给的是上限，实际批数取决于那个文件有多少句
#   （实测 60 只切出 5 批）。第一版硬编码 6，于是第二、三轮的红是假的。
#   够用就好：每批 5 次尝试，4 批 = 20 次，刚好越过绝对量门槛 → 后面的批才会被跳过。
MIN_BATCHES = 5
MAX_ATTEMPTS = 5

# ⚠ `Retry-After: 0` 是没用的：`llm.py:385` 写的是 `ra or 2.0 ** attempt`，
#   而 `0` 是假值 → 会掉回指数退避（2/4/8/16s），一轮要跑好几分钟。
#   给 1 秒，退避 ≈ 1 + U(0,1.5) ≈ 1.75s，六批加起来约 1 分钟。
RETRY_AFTER = "1"


def _err(cls, status, msg):
    return cls(msg, response=httpx.Response(
        status, headers={"retry-after": RETRY_AFTER},
        request=httpx.Request("POST", "https://x/v1")), body=None)


class Always429(LLMClient):
    """每一次尝试都撞 429。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._tmp = tempfile.mkdtemp(prefix="r429_cache_")
        self.cache_root = pathlib.Path(self._tmp)
        MADE.append(self._tmp)

        async def _create(**kw2):
            raise _err(openai.RateLimitError, 429, "Rate limit reached")

        self._client.chat.completions.create = _create


class Jitter429(LLMClient):
    """**只第一次尝试**撞 429，之后以别的可重试错误失败。

    模拟「偶尔抖一下」—— 每批只贡献 1 次 429，六批共 6 次，远低于 `>= 20`
    的绝对量门槛，**不该**触发熔断。
    """

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._tmp = tempfile.mkdtemp(prefix="r429j_cache_")
        self.cache_root = pathlib.Path(self._tmp)
        MADE.append(self._tmp)
        self._seen = {}

        async def _create(**kw2):
            # ⚠ 键必须**整条消息**的摘要，不能截断：系统提示词三批都一样，
            #   截前 200 字符会让所有批共用一个计数器 —— 于是「每批 429 一次」
            #   实际变成「全程只 429 一次」，测试比它写的弱（第一版就是这样，
            #   报告里 rate_limited=1 把它露出来了）。
            key = hashlib.md5(
                json.dumps(kw2.get("messages", []), sort_keys=True,
                           ensure_ascii=False).encode("utf-8")).hexdigest()
            n = self._seen.get(key, 0)
            self._seen[key] = n + 1
            if n == 0:
                raise _err(openai.RateLimitError, 429, "Rate limit reached")
            raise _err(openai.InternalServerError, 500, "upstream hiccup")

        self._client.chat.completions.create = _create


def run_once(client_cls, extra):
    R.LLMClient = client_cls
    tmp = tempfile.mkdtemp(prefix="r429_out_")
    MADE.append(tmp)
    argv = [
        "--lang", "es", "--model", "gemini-3.7-flash", "--effort", "high",
        "--limit-files", "1", "--limit-sentences", "60",
        "--out", tmp, "--tag", "r429_e2e",
        "--concurrency", "1", "--force", "--skip-preflight", "--no-correct-retry",
    ] + extra
    rc = R.main(argv)
    js = sorted(pathlib.Path(tmp).glob("report_*.json"))
    rep = json.loads(js[-1].read_text(encoding="utf-8")) if js else {}
    return rc, rep, pathlib.Path(tmp)


def ck(name, ok, detail=""):
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f"   ({detail})" if detail else ""))
    if not ok:
        FAILED.append(name)


try:
    print(f"（每批最多 {MAX_ATTEMPTS} 次尝试；批数由语料决定，先跑一轮量出来）\n")

    # ================= 第一轮：一直 429，必须熔断 =================
    print("=== 第一轮：一直 429（应熔断）===")
    rc, rep, out = run_once(Always429, [])
    n429 = rep.get("rate_limited", 0)
    NB = rep.get("batches") or 0          # 这一轮真正切出来的批数 —— 后面两轮拿它当基准
    ck(f"语料切出 >= {MIN_BATCHES} 批（这轮测试才有区分力）", NB >= MIN_BATCHES,
       f"实际 {NB} 批")
    ck("退出码是 3（交付包被失败批闸门拦下）", rc == 3, f"实际 {rc}")
    ck("stop_reason 是「429 熔断」", rep.get("stop_reason") == "429 熔断",
       f"stop_reason={rep.get('stop_reason')!r}")
    ck("**429 计数按尝试累计**（不是按批）", n429 >= 20,
       f"rate_limited={n429} —— 每批 {MAX_ATTEMPTS} 次，20 是熔断的绝对量门槛")
    ck("**后续批被跳过**（熔断的全部意义）", rep.get("skipped_by_budget", 0) >= 1,
       f"skipped_by_budget={rep.get('skipped_by_budget')}（共 {rep.get('batches')} 批）")
    ck("**停派发是真的停了：只派发了 4 批**", rep.get("calls") == 4,
       f"calls={rep.get('calls')} —— 第 4 批跑完刚好越过 20 次")
    ck("跳过的批进了可定位清单", len(rep.get("skipped_batches") or []) >= 1,
       f"{len(rep.get('skipped_batches') or [])} 条")
    ck("失败的批没落缓存（续跑会精确重试）", rep.get("cache_hits", 0) == 0,
       f"cache_hits={rep.get('cache_hits')}")
    ck("**没有 results.zip**（熔断连交付包一起打掉 —— 这是设计事实，要记住）",
       not (out / "results.zip").exists())

    # ================= 第二轮：逃生口 =================
    print("\n=== 第二轮：--allow-429（逃生口应放行）===")
    rc2, rep2, _ = run_once(Always429, ["--allow-429"])
    ck("逃生口接对了：熔断不触发（跳过为 0）",
       rep2.get("skipped_by_budget", 0) == 0,
       f"skipped_by_budget={rep2.get('skipped_by_budget')} —— 非 0 说明 "
       f"--allow-429 没接上 a.allow_429")
    ck("全部批都被派发了（一个不落）",
       rep2.get("batches") == NB and rep2.get("calls") == NB,
       f"批={rep2.get('batches')}（基准 {NB}）calls={rep2.get('calls')}")
    ck("stop_reason 为空（没停过）", not rep2.get("stop_reason"),
       f"stop_reason={rep2.get('stop_reason')!r}")
    ck("429 仍然计数（逃生口只关熔断，不关计数）",
       rep2.get("rate_limited", 0) >= 20, f"rate_limited={rep2.get('rate_limited')}")

    # ================= 第三轮：反向风险 —— 抖动不该被误判 =================
    print("\n=== 第三轮：每批只 429 一次（抖动，**不该**熔断）===")
    rc3, rep3, _ = run_once(Jitter429, [])
    n429_3 = rep3.get("rate_limited", 0)
    # 每批恰好贡献 1 次 429 —— 所以它应当**等于批数**。第一版的键被截断了，
    # 所有批共用一个计数器，这里只报 1，于是这一轮名不副实。
    ck("每批都真的 429 了一次（这一轮有区分力，不是空真）", n429_3 == NB,
       f"rate_limited={n429_3}，批数={NB}")
    ck("总 429 次数仍低于 20 的绝对量门槛（所以下面那条才说明问题）",
       n429_3 < 20, f"rate_limited={n429_3}")
    ck("**没触发熔断**（绝对量远低于 20 的门槛）",
       not rep3.get("stop_reason"), f"stop_reason={rep3.get('stop_reason')!r}")
    ck("全部批都被派发、一批都没被跳过",
       rep3.get("skipped_by_budget", 0) == 0 and rep3.get("calls") == NB,
       f"跳过={rep3.get('skipped_by_budget')} calls={rep3.get('calls')}（基准 {NB}）")
finally:
    for d in MADE:
        shutil.rmtree(d, ignore_errors=True)

print()
if FAILED:
    print(f"端到端失败 {len(FAILED)} 项：")
    for x in FAILED:
        print(f"  - {x}")
    sys.exit(1)
print("端到端通过：429 风暴真熔断、逃生口真放行、瞬时抖动不被误判")
