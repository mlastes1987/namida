# Tengai Makyou III: Namida English fan translation

This repository contains the tools and documentation for an in-progress English fan translation. The tools use a local dump of the Japanese PS2 disc. Generate the message files locally before drafting or building. A translated test build also requires a locally prepared translation file.

The export comes from the Japanese PS2 disc `SLPM-65952`, with `VER = 1.02` in `SYSTEM.CNF`. It covers the 637 message banks in `MESSAGE/MESARC.AFS`. Of those, 607 contain entries. The export includes 27,635 indexed entries, including 491 with empty Japanese text.

## Translation file

`messages.jsonl` is UTF-8 with one JSON object per line. Start with `ev0100mes` and `ev0101mes` for early dialogue. `btlmes` includes battle text, and `etcmes` includes character names.

- `id` combines the bank name and the zero-based index in its offset table. Keep repeated lines separate because they can come from different message slots.
- `japanese` holds the source text. Line breaks are encoded as JSON `\n`. Tags such as `{CTRL:11000000}` preserve the original four-byte control records in place.
- `ruby` records the original furigana in order. The base characters appear in `japanese` and their readings appear in `ruby`.
- `english` is available for translation.
- `offset` is relative to the decompressed bank. `raw_hex` is the original packed entry, including its terminator.

## English draft

The numbered event banks in `messages.jsonl` are hand-translated through `ev0323mes`. That file holds 1,063 hand-translated entries, including several system messages. Continue editorial review at `ev0324mes` in the numbered events.

`messages.draft.jsonl` combines the hand translations with a first English pass for every other nonblank text entry. The remaining 497 rows are 496 empty or whitespace-only sources and one font character reference. The `translation_status` field distinguishes `hand_translated`, `reused_translation` for exact source matches, `machine_draft`, `empty_source`, and `reference_charset`. Machine drafts still need contextual review, especially for names, text split by control codes, and choices. In-game text fit is untested.

The draft was generated headlessly using a locally cached Sugoi v4 model. To regenerate it to a new file with Python, CTranslate2, and SentencePiece, point `--model` at the local model directory containing `model.bin` and `spm`.

```powershell
python .\draft_namida.py messages.jsonl messages.fresh.draft.jsonl --model 'C:\path\to\local-model'
```

Working terms used in the draft include Namida (ナミダ), Ichiyo (壱与), Ushibozu (牛坊主), Tsugumi (ツグミ), Ichimotsu (イチモツ), Lady Himiko (ヒミコさま), Sacred Mirror (ご神鏡), Fire Priestess (火の巫女), Clan of Fire (火の一族), Spirit Flame (妖火), Himuka Orb (ヒムカの珠), and Lifebloom (万命草). The draft keeps `ryo` for 両 and `kagura` for 神楽.

## Reproduce the export

```powershell
python .\extract_namida.py 'Tengai Makyou III - Namida (Japan).iso' messages.fresh.jsonl
```

The script uses the Python standard library, reads the ISO directly, and requires a new output filename. It checks the decompressed sizes, compares the little- and big-endian copies of each offset table, and checks message boundaries against the packed terminators. The glyph mapping comes from the game's own `font.cmp`.

## Opening dialogue test ISO

`Namida English Test 01.iso` includes the 41 hand translations from `ev0100mes` through `ev0104mes`. It relocates messages within each bank. In-game screenshots show garbled dialogue because the opening event code still passes the original byte offsets to the message decoder.

The current builder keeps each original message offset and both offset tables. A short dictionary reference at each offset expands to the English text using the game's existing message decoder.

Test 02 showed readable text with fixed spacing in opening dialogue. The current code also patches the event renderer that positions those glyphs and the width table that measures speaker names. The builder wraps English at word boundaries and emits ordinary line breaks. The game's event dialogue handler pauses after every two lines. The revised renderer and line fit still need an in-game check.

Run the checks without writing an ISO.

```powershell
python -B .\build_namida_test.py 'Tengai Makyou III - Namida (Japan).iso' messages.jsonl
```

```powershell
python -B .\build_namida_test.py 'Tengai Makyou III - Namida (Japan).iso' messages.jsonl 'Namida English Test 03.iso'
```

The builder requires a new output filename. It checks the inserted text by decoding each rebuilt bank and confirms that the replacement members fit inside their original archive allocations.

Test 04 inserts the hand-translated entries through `ev0323mes`, along with the reviewed system and credits entries. The expanded build repacks the message archive within its original disc extent. Byte-identical members share storage so the larger English banks fit.

```powershell
python -B .\build_namida_test.py 'Tengai Makyou III - Namida (Japan).iso' messages.jsonl 'Namida English Test 04.iso' --all-reviewed
```

Text stored in other archives, executable strings, and text drawn into graphics or movies remain outside this pass.
