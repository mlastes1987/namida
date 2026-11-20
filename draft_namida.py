"""Generate a separate English draft with the local Sugoi v4 model."""

import argparse
from collections import defaultdict
import json
import os
from pathlib import Path
import re
import unicodedata


CONTROL = r"\{CTRL:[0-9A-F]{8}\}"
SEPARATOR = re.compile(f"({CONTROL}|\n)")
SPEAKER = re.compile(r"\{CTRL:11000000\}([^\n{}]+)\{CTRL:11000000\}")
JAPANESE = re.compile(r"[ぁ-ゖァ-ヺ㐀-鿿々]")
CHARSET_ID = "etcmes:0016"
TERMS = {
    "ナミダ": "Namida",
    "壱与": "Ichiyo",
    "牛坊主": "Ushibozu",
    "ツグミ": "Tsugumi",
    "イチモツ": "Ichimotsu",
    "シロモツ": "Shiromotsu",
    "クロモツ": "Kuromotsu",
    "トンカラリン": "Tonkararin",
    "ヒミコ様": "Lady Himiko",
    "ヒミコさま": "Lady Himiko",
    "ご神鏡": "Sacred Mirror",
    "神鏡": "Sacred Mirror",
    "火の巫女": "Fire Priestess",
    "火の一族": "Clan of Fire",
    "妖火": "Spirit Flame",
    "ヒムカの珠": "Himuka Orb",
    "万命草": "Lifebloom",
    "変化城": "Castle Henge",
    "湯の花": "Spring Blossom",
    "毛毛寺": "Momoji Temple",
    "神楽": "kagura",
}
TERM_PATTERN = re.compile("|".join(map(re.escape, sorted(TERMS, key=len, reverse=True))))


def known_translations(records):
    candidates = defaultdict(set)
    for record in records:
        source, english = record["japanese"], record["english"]
        if not english:
            continue
        candidates[source].add(english)
        for name, translation in zip(SPEAKER.findall(source), SPEAKER.findall(english)):
            candidates[name].add(translation)
    return {source: next(iter(values)) for source, values in candidates.items() if len(values) == 1}


def translate_segments(segments, model, device):
    import ctranslate2
    import sentencepiece

    tokenizer = sentencepiece.SentencePieceProcessor(model_file=str(model / "spm/spm.ja.nopretok.model"))
    decoder = sentencepiece.SentencePieceProcessor(model_file=str(model / "spm/spm.en.nopretok.model"))
    translator = ctranslate2.Translator(
        str(model), device=device, compute_type="float16" if device == "cuda" else "int8"
    )
    ordered = sorted(segments, key=lambda segment: (len(segment), segment))
    for start in range(0, len(ordered), 512):
        batch = ordered[start:start + 512]
        inputs = [TERM_PATTERN.sub(lambda match: TERMS[match[0]], item) for item in batch]
        tokenized = tokenizer.encode(inputs, out_type=str)
        output = translator.translate_batch(tokenized, beam_size=3, max_batch_size=64, max_decoding_length=160)
        for source, result in zip(batch, output, strict=True):
            draft = decoder.decode(result.hypotheses[0]).strip()
            yield source, draft.replace("\n", " ")
        print(f"Translated {min(start + len(batch), len(ordered)):,}/{len(ordered):,} unique segments", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists")
    if os.name == "nt":
        dll_handles = [os.add_dll_directory(path) for path in os.environ.get("NAMIDA_DLL_DIRS", "").split(";") if path]

    with args.source.open(encoding="utf-8") as source:
        records = [json.loads(line) for line in source]
    known = known_translations(records)
    segments = set()
    for record in records:
        source = record["japanese"]
        if record["id"] != CHARSET_ID and not record["english"] and source not in known:
            segments.update(
                part.strip()
                for part in SEPARATOR.split(source)[::2]
                if JAPANESE.search(part) and part.strip() not in known and part.strip() not in TERMS
            )
    print(f"Drafting {len(segments):,} unique segments on {args.device}", flush=True)
    generated = dict(translate_segments(segments, args.model, args.device))

    def render(part):
        key = part.strip()
        if not key:
            return part
        if key in known:
            return known[key]
        if key in TERMS:
            return TERMS[key]
        if key in generated:
            return generated[key] or key
        return unicodedata.normalize("NFKC", part)

    counts = defaultdict(int)
    with args.output.open("x", encoding="utf-8", newline="\n") as output:
        for record in records:
            source = record["japanese"]
            if record["english"]:
                status = "hand_translated"
            elif record["id"] == CHARSET_ID:
                status = "reference_charset"
            elif not source.strip():
                status = "empty_source"
            elif source in known:
                record["english"] = known[source]
                status = "reused_translation"
            else:
                parts = SEPARATOR.split(source)
                record["english"] = "".join(render(part) if index % 2 == 0 else part for index, part in enumerate(parts))
                status = "machine_draft"
            if status != "reference_charset" and re.findall(CONTROL, source) != re.findall(CONTROL, record["english"]):
                raise ValueError(f"Control tags changed in {record['id']}")
            counts[status] += 1
            record["translation_status"] = status
            output.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(dict(counts))


if __name__ == "__main__":
    main()
