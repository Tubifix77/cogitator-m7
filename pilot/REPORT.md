# Mechanicus knowledge-gap pilot — gemma4:12b

Date: 2026-10-11. Model: `gemma4:12b` (Ollama 0.40.2, RTX 3080). 41 questions, 7 topics.
Questions + sourced reference answers: [questions.json](questions.json). Raw answers:
[closed-book](answers_closed_gemma4_12b.json), [open-book](answers_open_gemma4_12b.json).
Grader: Claude, against the reference answers. Strict: a right name with invented details counts as partial.

## Result

| | Correct | Partial | "I don't know" | Wrong (confident) |
|---|---|---|---|---|
| **Closed book** (plain lore-expert prompt, no vault) | 1 | 9 | 1 | **30** |
| **Open book** (servitor persona + 7 draft vault records) | **41** | 0 | 0 | 0 |

### Closed book by topic

| Topic | Qs | Correct | Partial | Wrong / unknown |
|---|---|---|---|---|
| Schism of Mars (Heresy) | 8 | 0 | 0 | 8 (one "I don't know") |
| Treaty of Olympus | 4 | 0 | 3 | 1 |
| Forge World Ryza | 5 | 0 | 1 | 4 |
| Legio Ignatum | 7 | 0 | 0 | 7 |
| Stygies VIII | 5 | 0 | 1 | 4 |
| Belisarius Cawl | 5 | 1 | 2 | 2 |
| Recent: Arks of Omen (2023), *Archmagos* (2025) | 7 | 0 | 2 | 5 |

## Findings

1. **The gap is depth, not recency.** The model fails Heresy-era classics (Kelbor-Hal, the Fire Wasps,
   Ryza = plasma) as badly as 2023–2025 lore. It has the vocabulary of 40k, not the facts below the surface.
2. **It does not admit ignorance.** Told explicitly to say "I don't know" rather than guess, it did so
   once in 31 failures. Typical inventions: Perturabo as Fabricator-General, Sanguinius duelling Vashtorr,
   an author "Aaron Dembski-Polish", a novel "The Gatekeepers".
3. **Vault entries override the model's wrong beliefs.** With the archive, every one of the 30 wrong
   answers was corrected, including ones where the model had a confident prior (Ryza → bolters,
   Sanguinius). The explicit `CORRECTION:` lines worked; no answer reverted to the prior.
4. **The persona does not get in the way** once the binary-loop bug below is fixed: answers stay
   in character, quote the record and name it (40/41 cited correctly; N5 cited a non-existent record name).

## Caveats — read before trusting the 41/41

- **Teaching to the test.** The vault entries were written from the same facts the questions ask about,
  so open-book 41/41 proves the model *uses* the vault faithfully. It does not prove the vault answers
  questions it wasn't written for. Next test: held-out questions (same facts, different phrasing; and
  related facts not in the vault, to check it says "no record" instead of inventing).
- **Corrections leak.** Two answers repeated a correction unprompted ("Note: it is not Cawl-Prime"), i.e.
  rebutting a mistake the user never made. Harmless, but better phrased as background the model can
  use silently.
- **One run, one grader, deterministic sampling** (temperature 0). Indicative, not a benchmark.

## Scale: why option 2 (retrieval) is required

41 facts took 7,449 characters of vault. The stuffing budget is 12,000 characters, so it holds roughly
**65 facts of this density**. Given finding 1, useful Mechanicus coverage is hundreds to thousands of facts.
Retrieval (embed records with `nomic-embed-text`, load only the relevant ones per question) is not optional.

## Bug found during the pilot (fixed)

At temperature 0 the persona rule "binary chants (e.g. 01001111 01001101)" sent the model into an
endless binary loop: 700 tokens of `0100…` and no answer, never emitting a stop token. The console had
no reply-length cap, so the same could happen in the GUI. Fixed in `cogitator.html`: binary chant limited
to one short line, and `num_predict: 1024` caps every reply. The probe also uses `repeat_penalty 1.15`.

## Sources

Lexicanum and Fandom block automated fetches (HTTP 402); facts came via web-search summaries of those
pages plus directly fetched official Warhammer Community articles. Each question in `questions.json`
links its source. Vault drafts are paraphrased facts with sources noted, not copied text.

## Next steps

1. Held-out test set (see caveats) to measure generalisation and "no record" behaviour.
2. Retrieval for the vault (option 2), so it can grow past ~65 facts.
3. A persona rule for named specifics *not* in the archive (names, numbers, dates): say the archive holds
   no record rather than invent — trading some breadth for honesty. Worth testing, given finding 2.
4. Review `vault_drafts/` and copy what you approve into `vaults/`.
