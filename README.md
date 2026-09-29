# term-extractor-multilingual

Extracts bilingual term pairs from sentence-aligned interpreted speech, one
xlsx per input file. Chinese ↔ Spanish / French / Russian.

[简体中文](README.zh-CN.md)

## Install

```bash
pip install -r requirements.txt
```

`pymorphy3-dicts-ru` is required for Russian; the run refuses to start without it.

## Usage

```bash
# assertion suite — needs neither a corpus nor an API key
python -m getterms.tests

# end-to-end scripts — use the synthetic fixture in fixtures/
python e2e/e2e_delivery_columns.py

# a real run
python -m getterms.run --lang es --model gemini-3.7-flash --effort high \
    --concurrency 40 --out out/es
```

Corpus is read from `corpus/<lang>/` (override with `CORPUS_ROOT`). Set `API_KEY`,
or the provider-specific variable, or drop the key in `keys/`. `--dry-run` writes
nothing and calls nothing.

## Output

One `*_term.xlsx` per input file, sheet `terms`:

```
sent_id | src_text | tgt_text | term_src | term_tgt | types | note | term_src_dict | term_tgt_dict
```

`term_src` and `term_tgt` are verbatim slices of that row's two sentences — they
must be findable character for character. `term_src_dict` and `term_tgt_dict` are
the dictionary forms (ISO 10241-1). `types` is drawn from the ten term types;
`num` is never extracted. `note` is always empty.

## License

Source-visible. Other uses require prior written permission — see [LICENSE](LICENSE).
