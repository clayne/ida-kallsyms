from collections import namedtuple
import logging
import struct


def align_up(p, a):
    return p & ~(a - 1)


def align(p, a):
    return (p + (a - 1)) & ~(a - 1)


def try_parse_token_index(rodata, endianness, offset):
    index_fmt = endianness + "H"
    (index,) = struct.unpack(index_fmt, rodata[offset : offset + 2])
    assert index == 0, "The first token index must be 0"
    indices = [index]
    for _ in range(255):
        offset += 2
        (index,) = struct.unpack(index_fmt, rodata[offset : offset + 2])
        if index <= indices[-1]:
            return None  # Token indices must be monotonically increasing.
        indices.append(index)
    return indices


def find_token_indices(rodata, endianness):
    token_index_offset = 0
    while True:
        # kallsyms_token_index is an array of monotonically increasing 256
        # shorts, the first of which is 0. It is located right after
        # kallsyms_token_table, which is a sequence of null-terminated strings.
        # Therefore, look for 1+2 consecutive zeroes.
        token_index_offset = rodata.find(b"\x00\x00\x00", token_index_offset) + 1
        if token_index_offset == 0 or token_index_offset + 512 > len(rodata):
            break
        token_index = try_parse_token_index(rodata, endianness, token_index_offset)
        if token_index is not None:
            yield token_index_offset, token_index


def try_parse_token_table(rodata, token_index, start_offset, end_offset):
    tokens = []
    for i in range(256):
        token_start_offset = start_offset + token_index[i]
        if i == 255:
            # Last token ends at the end of the table.
            token_end_offset = end_offset
        else:
            # Other tokens end at the start of the next respective token.
            token_end_offset = start_offset + token_index[i + 1]
        token = rodata[token_start_offset:token_end_offset]
        if b"\x00" in token[:-1]:
            return None  # Tokens must be printable.
        if token[-1:] != b"\x00":
            return None  # Tokens must be null-terminated.
        if token[:-1] in tokens:
            return None  # Tokens must not repeat
        tokens.append(token[:-1])
    return tokens


def find_token_tables(rodata, token_index, token_index_offset):
    # kallsyms_token_table is a sequence of 256 null-terminated strings.
    # There may be some padding at the end; skip it.
    token_table_end_offset = token_index_offset
    while rodata[token_table_end_offset - 2 : token_table_end_offset - 1] == b"\x00":
        token_table_end_offset -= 1
    # Find the last token by looking for a trailing \0.
    last_token_offset = rodata.rfind(b"\x00", 0, token_table_end_offset - 1) + 1
    if last_token_offset == 0:
        return
    # The last kallsyms_token_index element corresponds to the last token.
    # Use that information to locate kallsyms_token_table.
    token_table_offset = last_token_offset - token_index[-1]
    if token_table_offset < 0:
        return
    token_table = try_parse_token_table(
        rodata, token_index, token_table_offset, token_table_end_offset
    )
    if token_table is not None:
        yield token_table_offset, token_table


def find_markers(rodata, marker_fmt, marker_size, marker_offset):
    first = True
    markers = []
    while True:
        # kallsyms_markers is an array of monotonically increasing offsets,
        # which starts with 0. It is aligned on an 8-byte boundary, so if the
        # element size is 4 bytes and their number is odd, it is zero-padded at
        # the end.
        (marker,) = struct.unpack(
            marker_fmt, rodata[marker_offset : marker_offset + marker_size]
        )
        if first:
            first = False
            if marker == 0 and marker_size == 4:
                # Skip padding.
                marker_offset -= marker_size
                continue
        elif len(markers) > 0 and marker >= markers[-1]:
            # The array is not monotonically increasing.
            return
        markers.append(marker)
        if marker == 0:
            # We found the first element.
            break
        marker_offset -= marker_size
    if marker_size == 4 and len(markers) == 2:
        # Marker size must be 8 bytes, and we must be taking the upper
        # part, which is always 0, for the first marker.
        return
    markers.reverse()
    yield marker_offset, markers


# Since v6.1 (commit b8a94bfb3395).
KSYM_NAME_LEN = 512


def is_name_ok(rodata, token_lengths, offset):
    n_tokens = ord(rodata[offset : offset + 1])
    if n_tokens == 0 or n_tokens >= KSYM_NAME_LEN:
        # Tokens are at least one byte long. Names must not be empty, and they
        # must be at most KSYM_NAME_LEN-1 characters long.
        return False
    offset += 1
    name_length = 0
    for _ in range(n_tokens):
        # The caller is expected to have verified that the name entry does not
        # span past the end of kallsyms_names, so just fetch the next token.
        name_length += token_lengths[ord(rodata[offset : offset + 1])]
        if name_length >= KSYM_NAME_LEN:
            # Name is longer than KSYM_NAME_LEN-1 characters.
            return False
        offset += 1
    return True


def extract_name(rodata, token_table, offset):
    # Name must have already been checked, just expand tokens.
    n_tokens = ord(rodata[offset : offset + 1])
    name = b""
    for _ in range(n_tokens):
        offset += 1
        name += token_table[ord(rodata[offset : offset + 1])]
    return name


def find_num_syms(rodata, endianness, token_table, markers_offset):
    # kallsyms_names is a sequence of length-prefixed entries ending with
    # padding to an 8-byte boundary, followed by kallsyms_markers.
    # Unfortunately, some guesswork is required to locate the start of
    # kallsyms_names given that we know the start of kallsyms_markers.
    num_syms_fmt = endianness + "I"
    token_lengths = [len(token) for token in token_table]
    # Indexed by (markers_offset - offset). Each element is a number of name
    # entries that follow the respective offset, or None if that offset is not
    # a start of a valid name entry.
    name_counts = [0]
    # Whether offset still points to one of the trailing zeroes.
    trailing_zeroes = True
    offset = markers_offset
    while offset >= 9:
        offset -= 1
        current_byte = ord(rodata[offset : offset + 1])
        if current_byte != 0:
            # Trailing zeroes have ended.
            trailing_zeroes = False
        next_name_offset = offset + current_byte + 1
        if next_name_offset > markers_offset:
            # The current name entry spans past the end of kallsyms_names. This
            # is allowed if we are still looking at trailing zeroes.
            name_counts.append(0 if trailing_zeroes else None)
            continue
        next_name_count = name_counts[markers_offset - next_name_offset]
        if next_name_count is None:
            # The next name entry is invalid, which means the current name
            # entry cannot be valid.
            name_counts.append(None)
            continue
        if is_name_ok(rodata, token_lengths, offset):
            # The current name entry is valid. Check whether it is preceded by
            # kallsyms_num_syms value, which is consistent with the number of
            # name entries we've seen so far.
            name_counts.append(next_name_count + 1)
            num_syms_offset = None
            # How kallsyms_num_syms is aligned depends on a particular kernel,
            # so try different offsets.
            for i in (-4, -8, -12, -16):
                (num_syms,) = struct.unpack(
                    num_syms_fmt, rodata[offset + i : offset + i + 4]
                )
                if name_counts[-1] == num_syms:
                    num_syms_offset = offset + i
                    break
                if num_syms != 0:
                    break
            if num_syms_offset is not None:
                break
        else:
            # The current name entry is not valid. This is allowed if we are
            # still looking at trailing zeroes.
            name_counts.append(0 if trailing_zeroes else None)
    else:
        return
    # We've found kallsyms_names, now parse it.
    names = []
    for _ in range(name_counts[-1]):
        names.append(extract_name(rodata, token_table, offset).decode())
        offset += ord(rodata[offset : offset + 1]) + 1
    yield num_syms_offset, names


Word = namedtuple("Word", ("size", "fmt", "ctype"))
WORD32 = Word(4, "I", "u32")
WORD64 = Word(8, "Q", "u64")


def find_addresses_no_kallsyms_base_relative(
    rodata, endianness, addresses_offset, num_syms, word
):
    if addresses_offset + num_syms * word.size > len(rodata):
        return
    address_fmt = endianness + word.fmt
    if word.size == 8 and addresses_offset % 8 != 0:
        addresses_offset -= 4
    offset = addresses_offset
    addresses = []
    for _ in range(num_syms):
        (address,) = struct.unpack(address_fmt, rodata[offset : offset + word.size])
        if len(addresses) > 0 and address < addresses[-1]:
            # The resulting addresses are not sorted.
            return
        addresses.append(address)
        offset += word.size
    logging.debug(
        "0x%08X: %s kallsyms_addresses[]",
        addresses_offset,
        word.ctype,
    )
    yield addresses_offset, offset, addresses


def find_addresses_kallsyms_base_relative(
    rodata, endianness, addresses_offset, num_syms, word
):
    if addresses_offset < 0:
        return
    addresses_end = addresses_offset + num_syms * 4
    relative_base_offset = align(addresses_end, word.size)
    relative_base_end = relative_base_offset + word.size
    if relative_base_end > len(rodata):
        return
    raw_addresses = struct.unpack(
        endianness + "i" * num_syms, rodata[addresses_offset:addresses_end]
    )
    (kallsyms_relative_base,) = struct.unpack(
        endianness + word.fmt,
        rodata[relative_base_offset:relative_base_end],
    )

    def log_ok():
        logging.debug(
            "0x%08X: %s kallsyms_relative_base=0x%016X",
            relative_base_offset,
            word.ctype,
            kallsyms_relative_base,
        )
        logging.debug("0x%08X: u32 kallsyms_offsets[]", addresses_offset)

    # Try !KALLSYMS_ABSOLUTE_PERCPU first.
    # A lot of small nonnegative numbers will match KALLSYMS_ABSOLUTE_PERCPU
    # too, but it's more likely to be !KALLSYMS_ABSOLUTE_PERCPU.
    addresses = []
    for raw in raw_addresses:
        address = kallsyms_relative_base + (raw & 0xFFFFFFFF)
        if len(addresses) > 0 and address < addresses[-1]:
            # The resulting addresses are not sorted.
            break
        addresses.append(address)
    else:
        log_ok()
        yield addresses_offset, addresses_end, addresses

    # Try KALLSYMS_ABSOLUTE_PERCPU.
    addresses = []
    for raw in raw_addresses:
        if raw >= 0:
            address = raw
        else:
            address = kallsyms_relative_base - 1 - raw
        if len(addresses) > 0 and address < addresses[-1]:
            # The resulting addresses are not sorted.
            break
        addresses.append(address)
    else:
        log_ok()
        yield addresses_offset, relative_base_end, addresses


def find_kallsyms_in_rodata(rodata):
    for addresses, names in (
        (addresses, names)
        for endianness in ("<", ">")
        for _ in (logging.debug("Endianness: %s", endianness),)
        for token_index_offset, token_index in find_token_indices(rodata, endianness)
        for _ in (
            logging.debug(
                "0x%08X: kallsyms_token_index=%s", token_index_offset, token_index
            ),
        )
        for token_table_offset, token_table in find_token_tables(
            rodata, token_index, token_index_offset
        )
        for _ in (
            logging.debug(
                "0x%08X: kallsyms_token_table=%s", token_table_offset, token_table
            ),
        )
        # In 6.2 (commits 60443c88f3a8 and 19bd8981dc2e) kallsyms_seqs_of_names
        # was added between kallsyms_markers and kallsyms_token_table.
        for markers_end_offset in range(token_table_offset, -4, -4)
        # In 4.20 the size of markers was reduced to 4 bytes.
        for marker_fmt, marker_size in (
            (endianness + "I", 4),
            (endianness + "Q", 8),
        )
        for markers_offset, markers in find_markers(
            rodata, marker_fmt, marker_size, markers_end_offset - marker_size
        )
        for _ in (
            logging.debug("0x%08X: kallsyms_markers=%s", markers_offset, markers),
        )
        for num_syms_offset, names in find_num_syms(
            rodata, endianness, token_table, markers_offset
        )
        for _ in (
            logging.debug("0x%08X: kallsyms_num_syms=%s", num_syms_offset, len(names)),
        )
        for word in (WORD64, WORD32)
        for _ in (logging.debug("WORD%d", word.size),)
        # KALLSYMS_BASE_RELATIVE means that kallsyms_offsets are followed by
        # kallsyms_relative_base. This was introduced in 4.6 by commit
        # 2213e9a66bb8.
        for base_relative in (False, True)
        for _ in (
            logging.debug("KALLSYMS_BASE_RELATIVE=%s", "y" if base_relative else "n"),
        )
        # Since kernel 6.4 (commit 404bad70fcf7) kallsyms_addresses come after
        # kallsyms_token_index.
        for addresses_first in (True, False)
        for _ in (logging.debug("addresses_first=%s", addresses_first),)
        for addresses_offset, addresses_end, addresses in (
            find_addresses_kallsyms_base_relative(
                rodata,
                endianness,
                (
                    align_up(
                        align_up(num_syms_offset, word.size)
                        - word.size
                        - len(names) * 4,
                        word.size,
                    )
                    if addresses_first
                    else align(token_index_offset + 512, word.size)
                ),
                len(names),
                word,
            )
            if base_relative
            else find_addresses_no_kallsyms_base_relative(
                rodata,
                endianness,
                (
                    num_syms_offset - len(names) * word.size
                    if addresses_first
                    else token_index_offset + 512
                ),
                len(names),
                word,
            )
        )
        for _ in (
            logging.debug(
                "0x%08X: kallsyms[0x%08X]",
                addresses_offset if addresses_first else num_syms_offset,
                (
                    token_index_offset + 512 - addresses_offset
                    if addresses_first
                    else (
                        align(addresses_end, word.size)
                        + len(names) * 3
                        - num_syms_offset
                    )
                ),
            ),
        )
    ):
        return zip(addresses, names)
    return []
