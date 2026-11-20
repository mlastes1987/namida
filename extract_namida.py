"""Export the SLPM-65952 message archive as UTF-8 JSON Lines."""

import argparse
from itertools import islice
import json
from pathlib import Path
import struct


SECTOR_SIZE = 2048


def iso_extent(iso, path):
    iso.seek(16 * SECTOR_SIZE)
    descriptor = iso.read(SECTOR_SIZE)
    if descriptor[:7] != b"\x01CD001\x01":
        raise ValueError("Expected an ISO 9660 primary volume descriptor")
    record = descriptor[156:]
    for part in path.split("/"):
        sector, size = struct.unpack_from("<I4xI", record, 2)
        iso.seek(sector * SECTOR_SIZE)
        directory = iso.read(size)
        cursor = 0
        while cursor < size:
            length = directory[cursor]
            if not length:
                cursor = (cursor // SECTOR_SIZE + 1) * SECTOR_SIZE
                continue
            record = directory[cursor:cursor + length]
            cursor += length
            name = record[33:33 + record[32]].split(b";")[0]
            if name == part.encode("ascii"):
                break
        else:
            raise ValueError(f"Missing ISO member {path}")
    sector, size = struct.unpack_from("<I4xI", record, 2)
    return sector * SECTOR_SIZE, size


def iso_member(iso, path):
    offset, size = iso_extent(iso, path)
    iso.seek(offset)
    return iso.read(size)


def afs_members(data):
    if not data.startswith(b"AFS\0"):
        raise ValueError("Expected an AFS archive")
    count, = struct.unpack_from("<I", data, 4)
    table_end = 8 + count * 8
    names, names_size = struct.unpack_from("<II", data, table_end)
    if names_size != count * 48 or names + names_size > len(data):
        raise ValueError("Invalid AFS name table")
    members = {}
    for index, (offset, size) in enumerate(struct.iter_unpack("<II", data[8:table_end])):
        start = names + index * 48
        name = data[start:start + 32].split(b"\0")[0].decode("ascii")
        if name in members or offset + size > len(data):
            raise ValueError(f"Invalid AFS member {name}")
        members[name] = data[offset:offset + size]
    return members


def decompress(data):
    # SLPM_659.52 EE routine at 0x0011AC28.
    if not data.startswith(b"CMP\0"):
        raise ValueError("Expected a CMP member")
    cursor = 8
    remaining = 0
    flags = 0
    output = bytearray()
    size, = struct.unpack_from("<I", data, 4)

    def byte():
        nonlocal cursor
        if cursor == len(data):
            raise ValueError("Truncated CMP stream")
        value = data[cursor]
        cursor += 1
        return value

    def bit():
        nonlocal remaining, flags
        if not remaining:
            flags = byte()
            remaining = 8
        remaining -= 1
        return (flags >> remaining) & 1

    while True:
        if bit():
            output.append(byte())
        else:
            if bit():
                count = byte()
                if not count:
                    break
                offset = -1
            else:
                count = bit() * 2 + bit()
                if count:
                    offset = byte() - 256
                else:
                    match = byte() | (byte() << 8)
                    offset = (match >> 4) - 4096
                    count = match & 15
            if -offset > len(output):
                raise ValueError('CMP match points before output')
            for _ in range(count + 1):
                output.append(output[offset])
        if len(output) > size:
            raise ValueError('CMP output exceeds declared size')
    if len(output) != size:
        raise ValueError('CMP output differs from declared size')
    return bytes(output)


def packed_strings(data):
    """Read byte-aligned, 12-bit token strings ending in 0xFFF."""
    cursor = 0
    while cursor < len(data):
        start = cursor
        tokens = []
        while cursor + 1 < len(data):
            first = (data[cursor] << 4) | (data[cursor + 1] >> 4)
            cursor += 2
            if first == 0xFFF:
                break
            tokens.append(first)
            if cursor == len(data):
                raise ValueError("Truncated second MD token")
            second = ((data[cursor - 1] & 0xF) << 8) | data[cursor]
            cursor += 1
            if second == 0xFFF:
                break
            tokens.append(second)
        else:
            raise ValueError("Missing MD string terminator")
        yield start, cursor, tokens


def render_message(tokens, glyphs, controls):
    japanese = []
    ruby = []
    cursor = 0
    glyph_count = len(glyphs)
    while cursor < len(tokens):
        token = tokens[cursor]
        cursor += 1
        if token < glyph_count:
            japanese.append(glyphs[token])
        elif token == 0xFFE:
            japanese.append("\n")
        elif token < glyph_count + len(controls):
            control = controls[token - glyph_count]
            if control.startswith("07"):
                base_end = cursor + int(control[2:4], 16)
                reading_end = base_end + int(control[4:6], 16)
                span = tokens[cursor:reading_end]
                if len(span) == reading_end - cursor and all(t < glyph_count for t in span):
                    base = "".join(glyphs[t] for t in tokens[cursor:base_end])
                    reading = "".join(glyphs[t] for t in tokens[base_end:reading_end])
                    ruby.append({"base": base, "reading": reading, "control": control})
                    japanese.append(base)
                    cursor = reading_end
                    continue
            japanese.append(f"{{CTRL:{control}}}")
        else:
            raise ValueError(f"Unexpected MD token 0x{token:X}")
    return "".join(japanese), ruby


def message_bank(data, offsets, glyphs):
    # SLPM_659.52 EE routine at 0x00410D58.
    if not data.startswith(b"MD"):
        raise ValueError("Expected an MD message bank")
    glyph_count, control_count, word_count, text_start = struct.unpack_from(">4H", data, 2)
    control_end = 10 + control_count * 4
    if glyph_count != len(glyphs) or not control_end <= text_start <= len(data):
        raise ValueError("Invalid MD header")
    controls = [data[i:i + 4].hex().upper() for i in range(10, control_end, 4)]
    dictionary_entries = list(islice(packed_strings(data[control_end:]), word_count))
    dictionary = [tokens for _, _, tokens in dictionary_entries]
    if len(dictionary) != word_count:
        raise ValueError("MD dictionary count mismatch")
    dictionary_end = control_end + (dictionary_entries[-1][1] if dictionary_entries else 0)
    if dictionary_end < text_start:
        raise ValueError("MD dictionary ends before the text section")
    if (offsets[0] if offsets else len(data)) != text_start:
        raise ValueError("MD offset table does not start at the text section")

    for index, start in enumerate(offsets):
        end = offsets[index + 1] if index + 1 < len(offsets) else len(data)
        if not text_start <= start < end <= len(data):
            raise ValueError(f"Invalid MD range at 0x{start:X}")
        raw = data[start:end]
        _, used, tokens = next(packed_strings(raw))
        if used != len(raw) and dictionary_end == text_start:
            raise ValueError(f"MD terminator does not match offset table at 0x{start:X}")
        expanded = []
        for token in tokens:
            if glyph_count + control_count <= token < 0xFFE:
                expanded.extend(dictionary[token - glyph_count - control_count])
            else:
                expanded.append(token)
        japanese, ruby = render_message(expanded, glyphs, controls)
        yield index, start, japanese, ruby, raw[:used].hex()


def export(members):
    font = decompress(members["font.cmp"])
    glyphs = []
    for first, second in struct.iter_unpack("BB", font):
        code = bytes((first, second)) if first else bytes((second,))
        try:
            glyphs.append(code.decode("cp932"))
        except UnicodeDecodeError:
            glyphs.append(f"{{GLYPH:{len(glyphs):03X}}}")

    for filename, compressed in members.items():
        if not filename.endswith(".cmp") or filename == "font.cmp":
            continue
        bank = filename.removesuffix(".cmp")
        offsets = [value for value, in struct.iter_unpack("<I", members[bank + ".ofs"])]
        offsets_big = [value for value, in struct.iter_unpack(">I", members[bank + ".ofs.big"])]
        if offsets != offsets_big:
            raise ValueError(f"{bank}: endian copies of the offset table differ")
        try:
            for index, offset, japanese, ruby, raw_hex in message_bank(decompress(compressed), offsets, glyphs):
                yield {
                    "id": f"{bank}:{index:04d}",
                    "offset": f"0x{offset:X}",
                    "japanese": japanese,
                    "english": "",
                    "ruby": ruby,
                    "raw_hex": raw_hex,
                }
        except (IndexError, ValueError) as error:
            raise ValueError(f"{bank}: {error}") from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("iso", type=Path)
    parser.add_argument("output", type=Path, help="New JSONL file")
    args = parser.parse_args()
    with args.iso.open("rb") as iso:
        members = afs_members(iso_member(iso, "MESSAGE/MESARC.AFS"))
    records = list(export(members))
    with args.output.open("x", encoding="utf-8", newline="\n") as output:
        for record in records:
            output.write(json.dumps(record, ensure_ascii=False) + "\n")
    banks = {record["id"].split(":")[0] for record in records}
    print(f"Exported {len(records):,} messages from {len(banks):,} nonempty banks to {args.output}")


if __name__ == "__main__":
    main()
