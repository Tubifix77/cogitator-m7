"""Knowledge-gap probe: ask a local model lore questions and record its answers for grading.

    python probe.py --mode closed                      # plain lore expert, no vault: what does it KNOW?
    python probe.py --mode open --vault vault_drafts   # real servitor persona + archive: do entries fix gaps?

Answers go to answers_<mode>_<model>.json next to this script. Grading is done separately
(by a human or a stronger model) against the reference answers in questions.json.
"""
import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
OLLAMA = "http://localhost:11434"

CLOSED_PROMPT = (
    "You are an expert on the official Warhammer 40,000 and Horus Heresy lore published by Games Workshop "
    "and Black Library. Answer the question briefly and factually, with names where asked. "
    "If you do not know or are not sure, say 'I don't know' instead of guessing."
)


def ollama(path, payload, timeout=600):
    req = urllib.request.Request(f"{OLLAMA}{path}", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.load(res)


def open_book_prompt(vault_dir):
    """The cogitator's real system prompt plus its archive, built exactly as the console builds it."""
    html = (HERE.parent / "cogitator.html").read_text(encoding="utf-8")
    persona = re.search(r"const SYSTEM_INSTRUCTION = `(.*?)`;", html, re.S).group(1)
    sys.path.insert(0, str(HERE.parent))
    import server  # noqa: E402  (reuse the server's own archive builder)
    server.VAULTS = Path(vault_dir).resolve()
    a = server.vault_archive()
    archive = "DATA-VAULT ARCHIVE (/STC_ARCHIVES/SACRED_LOGS/):\n" + "\n\n".join(
        f"=== RECORD: {r['name']} ===\n{r['content']}" for r in a["records"])
    if a["sealed"]:
        archive += f"\n\nSEALED RECORDS (exist, but the archive is too large to load them this cycle): {', '.join(a['sealed'])}"
    print(f"archive: {len(a['records'])} records, {a['chars']} chars, sealed: {a['sealed'] or 'none'}", flush=True)
    return persona + "\nHOST AUGURY: not relevant to lore queries.\n\n" + archive


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gemma4:12b")
    ap.add_argument("--mode", choices=["closed", "open"], default="closed")
    ap.add_argument("--vault", help="folder of vault records (open mode)")
    ap.add_argument("--ids", help="comma-separated question ids to run (default: all)")
    args = ap.parse_args()

    questions = json.loads((HERE / "questions.json").read_text(encoding="utf-8"))
    if args.ids:
        wanted = set(args.ids.split(","))
        questions = [q for q in questions if q["id"] in wanted]
    system = CLOSED_PROMPT if args.mode == "closed" else open_book_prompt(args.vault)

    results = []
    try:
        for i, q in enumerate(questions, 1):
            t = time.time()
            r = ollama("/api/chat", {
                "model": args.model, "stream": False, "think": False, "keep_alive": "10m",
                # Deterministic, but capped and repetition-penalised: at temperature 0 the persona
                # can lock into endless binary cant and never emit a stop token.
                "options": {"temperature": 0, "seed": 42, "num_ctx": 8192,
                            "num_predict": 600, "repeat_penalty": 1.15},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": q["q"]}]})
            answer = r["message"]["content"].strip()
            if r.get("done_reason") == "length":
                answer += "\n[TRUNCATED: hit num_predict cap]"
            results.append({"id": q["id"], "topic": q["topic"], "era": q["era"], "q": q["q"],
                            "ref": q["ref"], "answer": answer})
            print(f"[{i}/{len(questions)}] {q['id']} ({time.time() - t:.0f}s)", flush=True)
    finally:
        ollama("/api/generate", {"model": args.model, "keep_alive": 0})  # free the VRAM
        out = HERE / f"answers_{args.mode}_{args.model.replace(':', '_').replace('/', '_')}.json"
        out.write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"wrote {out.name} ({len(results)} answers)")


if __name__ == "__main__":
    main()
