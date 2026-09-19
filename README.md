# Term Extraction Pipeline

**Bilingual term tables from a sentence-aligned corpus — every term carries both its
verbatim span in the sentence and its dictionary form, and both go through their own
gate.**

[![README](https://img.shields.io/badge/README-%E4%B8%AD%E6%96%87-d73a49?style=for-the-badge)](README.zh-CN.md)

[![License](https://img.shields.io/badge/license-source--available-red)](LICENSE)
[![Term rows](https://img.shields.io/badge/term%20rows-210%2C394-blue)](#what-it-does)
[![Languages](https://img.shields.io/badge/languages-zh%20%E2%86%94%20es%2Ffr%2Fru-informational)](#what-it-does)

---

## What it does

Input: **a sentence-aligned bilingual corpus** (one source sentence paired with one
translation). Output: a set of `.xlsx` term tables — each row is one term, with its
**position in the original sentence**, its **dictionary form**, and **type tags**.

```
    aligned corpus
              │
              ▼
    ┌───────────────────┐
    │  8 pre-flight     │   fail ──▶ stop: nothing runs, nothing is charged
    │  gates            │
    └─────────┬─────────┘
              ▼
    ┌───────────────────┐
    │  dispatch         │   concurrency 40 · every response cached by prompt hash
    │                   │   breakers during the run: spend · 429 · balance
    └─────────┬─────────┘
              ▼
    ┌───────────────────┐
    │  verbatim anchor  │   not found ──▶ one corrective retry
    └─────────┬─────────┘   still missing ──▶ drop and record  (1 in 210,394)
              │ found
              ▼
    ┌───────────────────┐
    │  morphology       │   invalid ──▶ fall back to the surface form,
    │  validation       │               never drop the term
    └─────────┬─────────┘
              ▼
    ┌───────────────────┐
    │  9-column xlsx    │   verbatim span in 4/5 · dictionary form in 8/9
    └─────────┬─────────┘
              ▼
    ┌───────────────────┐
    │  delivery gate    │   over threshold ──▶ exit 3, no package
    └─────────┬─────────┘
              │ pass
              ▼
          results.zip
```

Scale actually run:

| | files | sentence pairs | term rows |
|---|---|---|---|
| Chinese ↔ Spanish | 531 | 56,110 | 71,097 |
| Chinese ↔ French | 554 | 65,097 | 69,719 |
| Chinese ↔ Russian | 533 | 57,469 | 69,578 |
| **total** | **1,618** | **178,676** | **210,394** |

## The hard part: two forms that cannot be derived from each other

A term has to satisfy two contradictory requirements at once:

- **It must be locatable in the source sentence.** Downstream inlines terms back
  into the text (`the [global economy]{term} is facing…`), so the program needs to
  find the term string in the sentence. It therefore has to be the **verbatim
  surface form** — changing even a punctuation mark puts it outside the sentence.
- **It must be usable in a term bank.** Term banks store the **dictionary form**:
  singular nouns, infinitive verbs, nominative case for languages with case.
  What appears in a sentence is inflected.

**Neither can be derived from the other** — going from surface form to dictionary
form needs morphological analysis (this pipeline does it); going the other way you
still need the surface form to locate anything. Measured divergence:

| language | columns 4/5 vs 8/9 differ | why |
|---|---|---|
| Spanish | 20.6% | number |
| French | 19.7% | number |
| **Russian** | **64.9%** | **6 cases restored to nominative** |

So the output carries **9 columns**: columns 4/5 give the verbatim span,
columns 8/9 give the dictionary form. A gate enforces that **columns 4/5 appear
verbatim in their sentence 100% of the time**.

## Anti-hallucination

- **Verbatim anchoring is a hard gate.** If a term the model returned cannot be
  located character-for-character in the sentence, it gets one retry and is then
  **dropped and recorded**. Across 210,394 rows, **1 was dropped**.
- **Morphological restoration is validated against a lemmatizer**
  (simplemma / pymorphy3), not taken on trust. Failures **fall back to the surface
  form and are recorded — terms are never dropped for this**.
- **A lexicon of habitually-plural terms** (`plural_lexicon.py`) with a cited
  dictionary or official source for every entry: `derechos humanos`,
  `Nations Unies`, `осадки` must not be squeezed into the singular.

## Engineering: eight gates and three circuit breakers

A full run takes hours and costs real money, so the design principle is
**stop rather than proceed wrongly**.

**Before** — is the morphology backend present → would output names collide → does
corpus completeness add up → are there stale artifacts in the output directory →
is the wall-clock budget exceeded → is there disk space → **does the key and model
actually work** (a live probe that sends one real request).

**During** — **spend breaker** (stops dispatching, does not kill in-flight
batches) → **429 breaker** (absolute count *and* ratio, so cold-start jitter does
not trip it) → **balance breaker** (stops on the first hit — it is terminal, not
transient).

**After** — if the failed-batch ratio exceeds a threshold, **no delivery package
is produced** (exit code 3) rather than shipping an incomplete one.

## Reproducibility

- **Every model response is cached**, keyed by model + effort + prompt hash + file
  md5 + batch content hash. Changing the **delivery layer** (column layout,
  normalization, ordering, packaging) does **not** change the key — a full
  re-export is measured at **zero API calls and 30 seconds**. Only a prompt change
  forces a re-run.
- **Resume**: re-running the same command after an interruption replays the
  completed batches from cache and is only charged for the rest.

## Verification

- **922 assertions** (`python -m getterms.tests`) in the internal copy, including
  four counter-example assertions for detectors that had already been falsified.
- **Six end-to-end scripts** (`e2e/`) that run the real pipeline with only the LLM
  client faked or the HTTP layer stubbed in — **zero API calls**. They cover
  delivery column placement, row ordering, and all three circuit breakers.
- **A delivery gate** (`getterms/verify.py`) that inspects every output file:
  headers, column names, the type whitelist, verbatim locatability, and whether
  each restored dictionary form is legal.

> The expensive part of this project was not writing the code — it was
> **proving the code was right**. After delivery, six read-only subagents
> re-inspected the output file by file; **four of their conclusions were
> themselves overturned**, including one of my own detectors.

## Layout

```
getterms/               the pipeline
  config.py             language registry, price table, paths
  corpus.py             reads zips / loose json, audits and repairs corpus anomalies
  extract.py            anchoring, validation, corrective retry, morphology, normalization
  llm.py                client, retry backoff, response cache
  run.py                main control: gate chain, dispatch, breakers, export
  verify.py             delivery gate
  writer.py             xlsx / zip / delivery-document generation
  selfcheck.py          zero-cost self-check over already-cached dumps
  plural_lexicon.py     habitually-plural lexicon, every entry sourced
  prompts/              three languages, 8 iterations each
  tests.py              assertion suite
e2e/                    end-to-end scripts, zero API calls
fixtures/               synthetic corpus fixture
```

## Quick start

```bash
pip install -r requirements.txt

# Assertion suite — needs no corpus and no key.
python -m getterms.tests

# End-to-end scripts — likewise: they use the synthetic fixture in fixtures/.
python e2e/e2e_delivery_columns.py
python e2e/e2e_row_order.py
python e2e/e2e_spend_breaker.py
python e2e/e2e_429_breaker.py
python e2e/e2e_balance_breaker.py
python e2e/destructive_cache_test.py

# A real run (needs a corpus and a key).
python -m getterms.run --lang es --model <model> --effort high \
    --concurrency 40 --out out/full_es --dry-run
```

## What is *not* in this repository

- **The corpus itself** — third-party content.
- **Model batch dumps** (the bake-off output) — they contain corpus sentences.
- **Keys.** A run needs a key file or an environment variable; the assertion suite
  and `e2e/` do **not** (they never send a request, so a dummy value is enough).
- **Deliverables** (xlsx / zip) — those contain the corpus too.

**No corpus, no model dumps, no keys, no deliverables.** That is why the assertion
suite here reports **540 passing** rather than 922: the corpus- and dump-dependent
blocks print a visible `[跳过] / skipped` line with the reason instead of silently
passing. The same code, with the corpus present, runs all 922.

## Three deliberate differences from the internal copy

1. `fixtures/` is a **synthetic, self-authored** corpus (a fictional city and
   museum), there only so that `e2e/` can run standalone.
2. `e2e/*.py` gained two things: the repo root on `sys.path`, and a dummy key.
   Both exist purely so that **anyone who clones this can run it**.
3. `tests.py` / `tests_fr.py` / `tests_ru.py` gained existence guards for the
   corpus and the dumps — missing, they **skip visibly** rather than crash or
   silently pass.

---

## License

**Source-available; all rights reserved.** Full text in [`LICENSE`](LICENSE).

You may view and study the source, and run it privately on your own hardware for
your own non-commercial evaluation or learning. **Everything else requires prior
written consent** — commercial use; institutional or systematic use (including
academic research, teaching, corpus construction, or dataset building);
redistribution, modification, or translation; or using this software or its output
to build, publish, or distribute any corpus or derived collection.

Some prompt files quote short examples from a third-party corpus; those remain the
property of their respective owners and are **not** covered by this license.
