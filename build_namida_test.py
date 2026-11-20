"""Build a Namida opening-dialogue test ISO from the Japanese original."""

import argparse
import json
from pathlib import Path
import re
import shutil
import struct

from extract_namida import afs_members, decompress, iso_extent, iso_member, message_bank, packed_strings


ARCHIVE = "MESSAGE/MESARC.AFS"
BANKS = ("ev0100mes", "ev0101mes", "ev0102mes", "ev0103mes", "ev0104mes")
CONTROL = re.compile(r"(\{CTRL:[0-9A-F]{8}\})")
EXECUTABLE = "SLPM_659.52"
# SLPM-65952 v1.02. The horizontal renderer loads these 96 byte widths
# from EE address 0x783210 and adds one unit of tracking.
WIDTH_TABLE_OFFSET = 0x684210
WIDTH_TABLE_SIZE = 96
DIALOGUE_WIDTH_TABLE_OFFSET = 0x684DF0
DIALOGUE_LINE_WIDTH = 430
SPEAKER_WIDTH = 122
BODY_TRACKING = 4
SPEAKER_TRACKING = 3
SPEAKER = re.compile(r"^(\{CTRL:11000000\})([^{}]+)(\{CTRL:11000000\})")
SPEAKER_SHORT_NAMES = {
    "Ichiyo's Friend": "Ichiyo's Pal", "Ichiyo's Father": "Dad",
    "Shrine Priest": "Priest", "Ferry Attendant": "Ferryman",
    "Departed Soul": "Spirit", "Haiku Master": "Poet",
    "Village Chief": "Chief", "Misato Baien": "Baien",
}


def font_characters(font):
    characters = []
    for first, second in struct.iter_unpack("BB", decompress(font)):
        code = bytes((first, second)) if first else bytes((second,))
        characters.append(code.decode("cp932"))
    return characters


def pack_message(english, glyph_ids, control_ids):
    tokens = []
    for part in CONTROL.split(english):
        if CONTROL.fullmatch(part):
            tokens.append(control_ids[bytes.fromhex(part[6:-1])])
        else:
            tokens.extend(0xFFE if char == "\n" else glyph_ids[char] for char in part)
    return pack_tokens(tokens)


def pack_tokens(tokens):
    tokens = [*tokens, 0xFFF]
    data = bytearray()
    for index in range(0, len(tokens), 2):
        first = tokens[index]
        if index + 1 == len(tokens):
            data.extend((first >> 4, (first & 15) << 4))
        else:
            second = tokens[index + 1]
            data.extend((first >> 4, ((first & 15) << 4) | (second >> 8), second & 255))
    return data


def font_widths(glyphs):
    widths = bytearray([12] * WIDTH_TABLE_SIZE)
    # Target advances, including the renderer's one-unit tracking.
    advances = {
        5: " !'.,:;|",
        6: "ijl[]()",
        7: '"ft-{}',
        8: "Irs/\\",
        9: "Jckvz*^",
        10: "abdeghnopquxy0123456789?+$<=>_~",
        11: "ABCDEFGHKLNOPQRSTUVXYZ&",
        12: "#",
        13: "mwMW%@",
    }
    for advance, characters in advances.items():
        for char in characters:
            widths[glyphs.index(char)] = advance - 1
    return bytes(widths)


def layout_dialogue(english, glyph_ids, widths):
    def measure(text, tracking=BODY_TRACKING):
        # Allow a full glyph cell at the right edge. Substitutions may expand.
        return sum(
            220 if CONTROL.fullmatch(part) else sum(
                widths[glyph_ids[char]] + tracking if glyph_ids[char] < 96 else 30
                for char in part
            )
            for part in CONTROL.split(text)
        )

    prefix = ""
    speaker = SPEAKER.match(english)
    if speaker:
        tag, name, _ = speaker.groups()
        if measure(name, SPEAKER_TRACKING) > SPEAKER_WIDTH:
            name = SPEAKER_SHORT_NAMES.get(name, name)
        if measure(name, SPEAKER_TRACKING) > SPEAKER_WIDTH:
            raise ValueError(f"Speaker name exceeds the dialogue box width: {name}")
        prefix = tag + name + tag
        english = english[speaker.end():]

    lines = []
    for source in english.split("\n"):
        line = ""
        for word in source.split(" "):
            candidate = f"{line} {word}" if line else word
            if line and measure(candidate) > DIALOGUE_LINE_WIDTH:
                lines.append(line)
                line = word
            else:
                line = candidate
            if measure(line) > DIALOGUE_LINE_WIDTH:
                raise ValueError(f"Unbreakable dialogue text exceeds line width: {word}")
        lines.append(line)
    # The event dialogue handler pauses after every second 0xFFE line break.
    return prefix + "\n".join(lines)


def patch_dialogue_renderer(elf, widths):
    # Add a small load segment in the ELF header padding, immediately below
    # the original text segment. The executable and disc extent keep their sizes.
    segment_offset = 0x100
    segment_address = 0x000FF100
    width_address = 0x783DF0
    hook = 0x41E9B0
    resume = 0x41E9C0

    def instruction(op, rs, rt, immediate):
        return (op << 26) | (rs << 21) | (rt << 16) | (immediate & 0xFFFF)

    def alu(rs, rt, rd, function, shift=0):
        return (rs << 21) | (rt << 16) | (rd << 11) | (shift << 6) | function

    def jump(address):
        return 0x08000000 | (address >> 2)

    # a0 contains the existing positional adjustment. ca counts glyphs on
    # this line. Sum preceding ASCII advances, or fall back for mixed text.
    code = [
        instruction(37, 30, 8, 0),        # lhu t0, 0(fp)
        instruction(11, 8, 8, 96),       # sltiu t0, t0, 96
        instruction(4, 8, 0, 18),        # beqz t0, fixed
        alu(5, 4, 9, 0x21),              # addu t1, a1, a0 (base X)
        instruction(4, 2, 0, 14),        # beqz v0, done
        alu(0, 2, 10, 0, 1),             # sll t2, v0, 1
        alu(30, 10, 10, 0x23),           # subu t2, fp, t2
        instruction(15, 0, 11, width_address >> 16),
        instruction(13, 11, 11, width_address),
        instruction(37, 10, 8, 0),       # loop: lhu t0, 0(t2)
        instruction(11, 8, 12, 96),
        instruction(4, 12, 0, 9),        # beqz t4, fixed
        alu(11, 8, 8, 0x21),
        instruction(36, 8, 8, 0),
        instruction(9, 8, 8, BODY_TRACKING),
        alu(9, 8, 9, 0x21),
        instruction(9, 10, 10, 2),
        instruction(5, 10, 30, -9),     # bne t2, fp, loop
        0,
        jump(resume),                    # done
        alu(9, 0, 2, 0x21),
        0x00A00013,                      # fixed: original mtlo/madd position
        0x70431000,
        jump(resume),
        0x00441021,
    ]
    # The nameplate precomputes positions separately. Measure the full ASCII
    # name for centering and sum the advances up to the current glyph.
    name_hook = 0x41DD90
    name_resume = 0x41DD9C
    name_address = segment_address + 4 * len(code)
    name_code = [
        alu(0, 23, 8, 0, 1),             # sll t0, s7, 1
        alu(18, 8, 8, 0x23),             # first glyph = s2 - 2 * s7
        instruction(15, 0, 9, width_address >> 16),
        instruction(13, 9, 9, width_address),
        alu(0, 0, 10, 0x21),             # total width
        alu(0, 0, 11, 0x21),             # prefix width
        instruction(37, 8, 12, 0),        # loop
        instruction(11, 12, 13, 96),
        instruction(4, 13, 0, 9),        # end of ASCII run
        alu(9, 12, 12, 0x21),
        instruction(36, 12, 12, 0),
        instruction(9, 12, 12, SPEAKER_TRACKING),
        alu(10, 12, 10, 0x21),
        alu(8, 18, 13, 0x2B),            # sltu t5, t0, s2
        alu(0, 13, 12, 0x0A),            # movz t4, zero, t5
        alu(11, 12, 11, 0x21),
        instruction(4, 0, 0, -11),
        instruction(9, 8, 8, 2),
        alu(8, 18, 13, 0x2B),            # fall back if current glyph is outside the run
        instruction(4, 13, 0, 6),
        alu(0, 10, 10, 2, 1),
        instruction(9, 11, 2, -237),
        alu(2, 10, 2, 0x23),
        jump(name_resume),
        0,
        0,
        0x00600013,                      # original mtlo v1
        jump(name_resume),
        0x72E21000,                      # original madd v0, s7, v0
    ]
    code.extend(name_code)
    payload = struct.pack(f"<{len(code)}I", *code)
    result = bytearray(elf)
    phoff, = struct.unpack_from("<I", elf, 28)
    phentsize, phnum = struct.unpack_from("<HH", elf, 42)
    second = phoff + phentsize
    if phentsize != 32 or phnum != 1 or any(elf[second:second + 32]):
        raise ValueError("Unexpected ELF program headers")
    if any(elf[segment_offset:segment_offset + len(payload)]):
        raise ValueError("ELF header padding is occupied")
    struct.pack_into("<8I", result, second, 1, segment_offset, segment_address,
                     segment_address, len(payload), len(payload), 5, 0x1000)
    struct.pack_into("<H", result, 44, phnum + 1)
    result[segment_offset:segment_offset + len(payload)] = payload
    hook_offset = hook - 0xFF000
    if elf[hook_offset:hook_offset + 16] != bytes.fromhex("1300a000 d2032492 00104370 21104400"):
        raise ValueError("Unexpected dialogue renderer instructions")
    struct.pack_into("<I", result, hook_offset, jump(segment_address))
    name_offset = name_hook - 0xFF000
    if elf[name_offset:name_offset + 12] != bytes.fromhex("13006000 603fc624 0010e272"):
        raise ValueError("Unexpected nameplate renderer instructions")
    struct.pack_into("<I", result, name_offset, jump(name_address))
    for offset in (WIDTH_TABLE_OFFSET, DIALOGUE_WIDTH_TABLE_OFFSET):
        if elf[offset:offset + len(widths)] != bytes([12] * len(widths)):
            raise ValueError("Unexpected original font width table")
        result[offset:offset + len(widths)] = widths
    return bytes(result)


def pad_dictionary(size):
    # The original decoder skips dictionary entries at byte-aligned terminators.
    # Empty and one-token entries occupy two and three bytes respectively.
    if size == 1 or size < 0:
        raise ValueError(f"Cannot fill a {size}-byte MD dictionary gap")
    if not size:
        return b""
    # A long dummy string costs one dictionary index, regardless of its size.
    remainder = size % 3
    if remainder == 1:
        return pack_tokens([]) + pad_dictionary(size - 2)
    tokens = 2 * (size // 3) - (remainder == 0)
    return pack_tokens([0] * tokens)


def preserve_offsets(original, offsets, packed_messages):
    glyph_count, control_count, original_count, text_start = struct.unpack_from(">4H", original, 2)
    dictionary_base = glyph_count + control_count
    control_end = 10 + control_count * 4
    original_dictionary = original[control_end:text_start]
    slots = [end - start for start, end in zip(offsets, offsets[1:] + [len(original)])]
    if any(size < 3 and payload is not None for size, payload in zip(slots, packed_messages, strict=True)):
        raise ValueError("A source message slot is too small for a dictionary reference")
    padding = [pad_dictionary(0 if size == 4 else size - 3) if payload is not None else b""
               for size, payload in zip(slots, packed_messages, strict=True)]
    dictionary_index = original_count + len(slots) + sum(
        sum(1 for _ in packed_strings(gap)) for gap in padding
    )
    first_payload = dictionary_base + dictionary_index
    empty_entry = first_payload + len(packed_messages)
    if empty_entry >= 0xFFE:
        raise ValueError("MD dictionary references exceed the token range")

    # The stubs occupy the original message slots. The game's dictionary
    # walker skips them and expands the real strings stored after those slots.
    stubs = bytearray()
    for index, (offset, size, gap, payload) in enumerate(zip(offsets, slots, padding, packed_messages, strict=True)):
        if payload is None:
            stubs.extend(original[offset:offset + size])
            continue
        reference = first_payload + index
        # A second reference to an empty entry fills a four-byte slot.
        tokens = [reference, empty_entry] if size == 4 else [reference]
        stubs.extend(pack_tokens(tokens) + gap)
    dictionary = original_dictionary + stubs + b"".join(
        payload if payload is not None else pack_tokens([]) for payload in packed_messages
    ) + pack_tokens([])
    count = dictionary_index + len(packed_messages) + 1
    if sum(1 for _ in packed_strings(dictionary)) != count:
        raise ValueError("MD dictionary layout mismatch")
    rebuilt = bytearray(original[:control_end])
    struct.pack_into(">H", rebuilt, 6, count)
    return rebuilt + dictionary


def compress(data):
    output = bytearray(b"CMP\0" + struct.pack("<I", len(data)))
    mask = 0
    flags = 0

    def bits(value, width):
        nonlocal mask, flags
        for shift in range(width - 1, -1, -1):
            if not mask:
                flags = len(output)
                output.append(0)
                mask = 0x80
            if (value >> shift) & 1:
                output[flags] |= mask
            mask >>= 1

    cursor = 0
    while cursor < len(data):
        # Search longest-first. CMP long matches reach back 4096 bytes.
        for length in range(min(16, len(data) - cursor), 2, -1):
            match = data.rfind(data[cursor:cursor + length], max(0, cursor - 4096), cursor)
            if match >= 0:
                distance = cursor - match
                if length <= 4 and distance <= 256:
                    bits(length - 1, 4)
                    output.append(256 - distance)
                else:
                    bits(0, 4)
                    output.extend(struct.pack("<H", ((4096 - distance) << 4) | (length - 1)))
                cursor += length
                break
        else:
            bits(1, 1)
            output.append(data[cursor])
            cursor += 1
    bits(1, 2)
    output.append(0)
    return bytes(output)


def patch_bank(bank, records, members, glyphs):
    original = decompress(members[bank + ".cmp"])
    offsets = [value for value, in struct.iter_unpack("<I", members[bank + ".ofs"])]
    glyph_count, control_count, _, text_start = struct.unpack_from(">4H", original, 2)
    control_ids = {
        original[10 + index * 4:14 + index * 4]: glyph_count + index
        for index in range(control_count)
    }
    glyph_ids = {char: index for index, char in reversed(list(enumerate(glyphs)))}
    packed_messages = []
    expected = []
    for index, offset, japanese, _, raw_hex in message_bank(original, offsets, glyphs):
        key = f"{bank}:{index:04d}"
        record = records[key]
        english = record["english"]
        if record["raw_hex"] != raw_hex or record["japanese"] != japanese:
            raise ValueError(f"Missing or mismatched translation for {key}")
        if not english:
            packed_messages.append(None)
            expected.append(japanese)
            continue
        if CONTROL.findall(english) != CONTROL.findall(japanese):
            raise ValueError(f"Control tags differ in {key}")
        try:
            if bank not in ("etcmes", "staffmes"):
                english = layout_dialogue(english, glyph_ids, font_widths(glyphs))
            packed = pack_message(english, glyph_ids, control_ids)
        except KeyError as error:
            raise ValueError(f"Unsupported character or control in {key}: {error}") from error
        packed_messages.append(packed)
        expected.append(english)

    rebuilt = preserve_offsets(original, offsets, packed_messages)
    compressed = compress(rebuilt)
    decoded = [text for _, _, text, _, _ in message_bank(decompress(compressed), offsets, glyphs)]
    if decoded != expected:
        raise ValueError(f"Round-trip verification failed for {bank}")
    return {
        bank + ".cmp": compressed,
    }


def patch_archive(original, replacements):
    archive = bytearray(original)
    count, = struct.unpack_from("<I", archive, 4)
    names, _ = struct.unpack_from("<II", archive, 8 + count * 8)
    entries = list(struct.iter_unpack("<II", archive[8:8 + count * 8]))
    boundaries = sorted({offset for offset, size in entries if size} | {names})
    remaining = dict(replacements)
    for index, (offset, old_size) in enumerate(entries):
        name_start = names + index * 48
        name = archive[name_start:name_start + 32].split(b"\0")[0].decode("ascii")
        if name not in remaining:
            continue
        payload = remaining.pop(name)
        limit = boundaries[boundaries.index(offset) + 1]
        if len(payload) > limit - offset:
            raise ValueError(f"{name} exceeds its AFS sector allocation")
        archive[offset:offset + max(old_size, len(payload))] = payload.ljust(max(old_size, len(payload)), b"\0")
        struct.pack_into("<I", archive, 12 + index * 8, len(payload))
    if remaining:
        raise ValueError(f"Missing AFS members: {', '.join(remaining)}")
    return archive


def repack_archive(original, members):
    """Repack sector-aligned members and share byte-identical payloads."""
    archive = bytearray(original)
    count, = struct.unpack_from("<I", archive, 4)
    table_end = 8 + count * 8
    names, _ = struct.unpack_from("<II", archive, table_end)
    cursor, = struct.unpack_from("<I", archive, 8)
    archive[cursor:names] = bytes(names - cursor)
    stored = {}
    for index, payload in enumerate(members.values()):
        if not payload:
            continue
        if payload not in stored:
            if cursor + len(payload) > names:
                raise ValueError("Repacked AFS exceeds its original disc allocation")
            stored[payload] = cursor
            archive[cursor:cursor + len(payload)] = payload
            cursor += (len(payload) + 2047) // 2048 * 2048
        struct.pack_into("<II", archive, 8 + index * 8, stored[payload], len(payload))
    return archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("iso", type=Path)
    parser.add_argument("translations", type=Path)
    parser.add_argument("output", type=Path, nargs="?", help="New test ISO. Omit to verify without building.")
    parser.add_argument("--all-reviewed", action="store_true", help="Insert every nonblank English entry")
    args = parser.parse_args()
    with args.translations.open(encoding="utf-8") as source:
        records = {row["id"]: row for row in map(json.loads, source)}

    with args.iso.open("rb") as original:
        if b"SLPM_659.52" not in iso_member(original, "SYSTEM.CNF"):
            raise ValueError("Expected Japanese release SLPM-65952")
        offset, size = iso_extent(original, ARCHIVE)
        original.seek(offset)
        archive = original.read(size)
        members = afs_members(archive)
        glyphs = font_characters(members["font.cmp"])
        banks = tuple(dict.fromkeys(key.split(":")[0] for key, row in records.items() if row["english"])) if args.all_reviewed else BANKS
        replacements = {}
        for bank in banks:
            replacements.update(patch_bank(bank, records, members, glyphs))
        expected = members | replacements
        rebuilt = repack_archive(archive, expected) if args.all_reviewed else patch_archive(archive, replacements)
        if len(rebuilt) != size or afs_members(rebuilt) != expected:
            raise ValueError("AFS verification failed")

        elf_offset, elf_size = iso_extent(original, EXECUTABLE)
        original.seek(elf_offset)
        widths = font_widths(glyphs)
        executable = patch_dialogue_renderer(original.read(elf_size), widths)
        if args.output is None:
            print(f"Verified {len(banks)} banks at original offsets and {len(set(widths))} distinct glyph advances")
            return

        original.seek(0)
        with args.output.open("x+b") as output:
            shutil.copyfileobj(original, output, 8 * 1024 * 1024)
            output.seek(offset)
            output.write(rebuilt)
            if afs_members(iso_member(output, ARCHIVE)) != expected:
                raise ValueError("Test ISO archive verification failed")
            output.seek(elf_offset)
            output.write(executable)
            if iso_member(output, EXECUTABLE) != executable:
                raise ValueError("Test ISO executable verification failed")
    print(f"Verified {len(banks)} translated banks in {args.output}")


if __name__ == "__main__":
    main()
