"""Self-contained QR encoder -> SVG, plus an independent decoder used as a self-test.

No third-party dependencies. Byte mode only, which is all this page needs.
"""
import sys

# ---------------------------------------------------------------- GF(256) / RS
EXP = [0] * 512
LOG = [0] * 256
_x = 1
for _i in range(255):
    EXP[_i] = _x
    LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    EXP[_i] = EXP[_i - 255]

MAX_LEN = 30  # reachable for byte mode + ECC level M


def gf_mul(a, b):
    if a == 0 or b == 0:
        return 0
    return EXP[LOG[a] + LOG[b]]


def rs_generator(n):
    g = [1]
    for i in range(n):
        # multiply g by (x - a^i)
        ng = [0] * (len(g) + 1)
        for j, c in enumerate(g):
            ng[j] ^= c
            ng[j + 1] ^= gf_mul(c, EXP[i])
        g = ng
    return g


def rs_remainder(data, n):
    g = rs_generator(n)
    # g is monic, len(g) == n + 1; keep it low-order-first like data
    rem = list(data) + [0] * n
    for i in range(len(data)):
        coef = rem[i]
        if coef == 0:
            continue
        for j in range(len(g)):
            rem[i + j] ^= gf_mul(g[j], coef)
    return rem[len(data):]


# --------------------------------------------------------- version / ECC tables
# Reed-Solomon block structure for error-correction level M, as a transcription of
# ISO/IEC 18004 (cross-checked against Thonky's published table by verify_rs_table.py).
#
#   version: (ec_codewords_per_block, group1_blocks, group2_blocks)
#
# Group 1 blocks hold floor(data_total / nblocks) data codewords; group 2 blocks hold
# exactly one more. ec_blocks() recomputes the per-block data lengths from the total
# so only the block counts and the EC length are stored here.
RS_TABLE = {
    1: (10, 1, 0), 2: (16, 1, 0), 3: (26, 1, 0), 4: (18, 2, 0), 5: (24, 2, 0),
    6: (16, 4, 0), 7: (18, 4, 0), 8: (22, 2, 2), 9: (22, 3, 2), 10: (26, 4, 1),
    11: (30, 1, 4), 12: (22, 6, 2), 13: (22, 8, 1), 14: (24, 4, 5), 15: (24, 5, 5),
    16: (28, 7, 3), 17: (28, 10, 1), 18: (26, 9, 4), 19: (26, 3, 11), 20: (26, 3, 13),
    21: (26, 17, 0), 22: (28, 17, 0), 23: (28, 4, 14), 24: (28, 6, 14), 25: (28, 8, 13),
    26: (28, 19, 4), 27: (28, 22, 3), 28: (28, 3, 23), 29: (28, 21, 7), 30: (28, 19, 10),
    31: (28, 2, 29), 32: (28, 10, 23), 33: (28, 14, 21), 34: (28, 14, 23), 35: (28, 12, 26),
    36: (28, 6, 34), 37: (28, 29, 14), 38: (28, 13, 32), 39: (28, 40, 7), 40: (28, 18, 31),
}


def raw_data_modules(version):
    """Count of data-capable modules (ISO/IEC 18004). Excludes function patterns,
    format information and, from version 7, the version information blocks."""
    result = (16 * version + 128) * version + 64
    if version >= 2:
        numalign = version // 7 + 2
        result -= (25 * numalign - 10) * numalign - 55
        if version >= 7:
            result -= 36
    return result


def total_codewords(version):
    return raw_data_modules(version) // 8


def ec_blocks(version):
    ec, g1, g2 = RS_TABLE[version]
    total = total_codewords(version)
    data_total = total - ec * (g1 + g2)
    g1_len = data_total // (g1 + g2)
    g2_len = g1_len + 1
    blocks = [g1_len] * g1 + [g2_len] * g2
    assert sum(blocks) + ec * (g1 + g2) == total, "RS table inconsistent for v%d" % version
    return ec, blocks


def alignment_positions(version):
    if version == 1:
        return []
    n = version // 7 + 2
    size = version * 4 + 17
    step = (size - 13 + n - 1) // (n - 1) if n > 1 else 0
    step = step + 1 if step % 2 else step
    positions = [6]
    pos = size - 7
    for _ in range(n - 1):
        positions.append(pos)
        pos -= step
    # first coordinate is 6, last is size-7; middle ones ordered ascending
    return [6] + sorted(positions[1:])


# ------------------------------------------------------------------- bit stream
def encode_payload(text, version):
    data = text.encode("utf-8")
    ec, blocks = ec_blocks(version)
    data_total = sum(blocks)
    capacity = data_total * 8
    bits = []

    def push(value, length):
        for i in range(length - 1, -1, -1):
            bits.append((value >> i) & 1)

    push(0b0100, 4)                 # byte mode
    if version <= 9:
        push(len(data), 8)
    elif version <= 26:
        push(len(data), 16)
    else:
        push(len(data), 16)
    for byte in data:
        push(byte, 8)

    remaining = capacity - len(bits)
    if remaining < 0:
        raise ValueError("payload does not fit version %d" % version)
    push(0, min(4, remaining))
    while len(bits) % 8:
        bits.append(0)
    pad = [0xEC, 0x11]
    i = 0
    while len(bits) < capacity:
        push(pad[i % 2], 8)
        i += 1

    codewords = []
    for i in range(0, len(bits), 8):
        v = 0
        for b in bits[i:i + 8]:
            v = (v << 1) | b
        codewords.append(v)

    # split into blocks, add Reed-Solomon per block
    chunks, idx = [], 0
    for length in blocks:
        chunks.append(codewords[idx:idx + length])
        idx += length
    ec_chunks = [rs_remainder(c, ec) for c in chunks]

    # interleave
    out = []
    for i in range(max(blocks)):
        for c in chunks:
            if i < len(c):
                out.append(c[i])
    for i in range(ec):
        for c in ec_chunks:
            out.append(c[i])
    return out


# ------------------------------------------------------------------- matrix
def make_matrix(version, data_bits):
    size = version * 4 + 17
    return [[None] * size for _ in range(size)], size


def reserve_function_patterns(version):
    """Return (matrix, reserved) with all function patterns and format/version areas marked."""
    size = version * 4 + 17
    m = [[None] * size for _ in range(size)]

    def setm(r, c, v):
        if 0 <= r < size and 0 <= c < size:
            m[r][c] = v

    # finder patterns + separators
    for (br, bc) in ((0, 0), (0, size - 7), (size - 7, 0)):
        for r in range(-1, 8):
            for c in range(-1, 8):
                rr, cc = br + r, bc + c
                if not (0 <= rr < size and 0 <= cc < size):
                    continue
                if 0 <= r <= 6 and 0 <= c <= 6:
                    edge = r in (0, 6) or c in (0, 6)
                    core = 2 <= r <= 4 and 2 <= c <= 4
                    setm(rr, cc, 1 if (edge or core) else 0)
                else:
                    setm(rr, cc, 0)  # separator

    # timing patterns
    for i in range(8, size - 8):
        m[6][i] = 1 if i % 2 == 0 else 0
        m[i][6] = 1 if i % 2 == 0 else 0

    # alignment patterns
    positions = alignment_positions(version)
    for r in positions:
        for c in positions:
            if (r <= 8 and c <= 8) or (r <= 8 and c >= size - 9) or (r >= size - 9 and c <= 8):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    # ring (outer border) = dark, middle ring = light, center = dark
                    dist = max(abs(dr), abs(dc))
                    m[r + dr][c + dc] = 1 if (dist == 2 or dist == 0) else 0

    # dark module
    m[size - 8][8] = 1

    # reserve format information areas (value filled later)
    fmt = []
    for i in range(9):
        if i != 6:
            fmt.append((8, i))
    for i in range(8):
        if i != 6:
            fmt.append((i, 8))
    for pos in fmt:
        if m[pos[0]][pos[1]] is None:
            m[pos[0]][pos[1]] = 0
    for i in range(8):
        if m[8][size - 1 - i] is None:
            m[8][size - 1 - i] = 0
    for i in range(7):
        if m[size - 1 - i][8] is None:
            m[size - 1 - i][8] = 0

    # version information blocks (version >= 7)
    if version >= 7:
        for i in range(18):
            r, c = i // 3, i % 3
            if m[r][size - 11 + c] is None:
                m[r][size - 11 + c] = 0
            if m[size - 11 + c][r] is None:
                m[size - 11 + c][r] = 0
    return m, size


def function_mask(version):
    """True where the module is a function/format module (not data)."""
    base, size = reserve_function_patterns(version)
    mask = [[False] * size for _ in range(size)]
    for r in range(size):
        for c in range(size):
            if base[r][c] is not None:
                mask[r][c] = True
    if version >= 7:
        for i in range(18):
            r, c = i // 3, i % 3
            mask[r][size - 11 + c] = True
            mask[size - 11 + c][r] = True
    return mask, base, size


MASK_FUNCS = [
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
]

FORMAT_EC_M = 0b00  # ECC level M


def format_info(mask_id):
    data = (FORMAT_EC_M << 3) | mask_id
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return ((data << 10) | rem) ^ 0x5412


def version_info(version):
    rem = version
    for _ in range(12):
        rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
    return (version << 12) | rem


def place_data(version, codewords, mask_id):
    mask, base, size = function_mask(version)
    m = [row[:] for row in base]
    bits = []
    for cw in codewords:
        for i in range(7, -1, -1):
            bits.append((cw >> i) & 1)

    idx = 0
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for row in rows:
            for cc in (col, col - 1):
                if mask[row][cc]:
                    continue
                bit = bits[idx] if idx < len(bits) else 0
                idx += 1
                if MASK_FUNCS[mask_id](row, cc):
                    bit ^= 1
                m[row][cc] = bit
        upward = not upward
        col -= 2

    # format information
    fi = format_info(mask_id)
    for i in range(15):
        bit = (fi >> i) & 1
        if i < 6:
            m[8][i] = bit
        elif i == 6:
            m[8][7] = bit
        elif i == 7:
            m[8][8] = bit
        elif i == 8:
            m[7][8] = bit
        else:
            m[14 - i][8] = bit
    for i in range(15):
        bit = (fi >> i) & 1
        if i < 8:
            m[size - 1 - i][8] = bit
        else:
            m[8][size - 15 + i] = bit
    # dark module stays 1
    m[size - 8][8] = 1

    if version >= 7:
        vi = version_info(version)
        for i in range(18):
            bit = (vi >> i) & 1
            r, c = i // 3, i % 3
            m[r][size - 11 + c] = bit
            m[size - 11 + c][r] = bit
    return m, size


def penalty(m, size):
    score = 0
    # rule 1: runs of 5+
    for line in list(m) + [[m[r][c] for r in range(size)] for c in range(size)]:
        run = 1
        for i in range(1, size):
            if line[i] == line[i - 1]:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run = 1
        if run >= 5:
            score += 3 + (run - 5)
    # rule 2: 2x2 blocks
    for r in range(size - 1):
        for c in range(size - 1):
            v = m[r][c]
            if v == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                score += 3
    # rule 3: 1011101 patterns
    pat1 = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0]
    pat2 = [0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1]
    lines = [row[:] for row in m] + [[m[r][c] for r in range(size)] for c in range(size)]
    for line in lines:
        for i in range(size - 10):
            seg = line[i:i + 11]
            if seg == pat1 or seg == pat2:
                score += 40
    # rule 4: dark/light balance
    dark = sum(sum(row) for row in m)
    total = size * size
    ratio = dark * 100 // total
    score += 10 * (abs(ratio - 50) // 5)
    return score


def build(text):
    data_len = len(text.encode("utf-8"))
    version = None
    for v in range(1, 41):
        ec, blocks = ec_blocks(v)
        cc_indicator = 8 if v <= 9 else 16
        needed = 4 + cc_indicator + data_len * 8
        if needed <= sum(blocks) * 8 and v <= MAX_LEN:
            version = v
            break
    if version is None:
        raise ValueError("text too long")
    codewords = encode_payload(text, version)
    best = None
    for mask_id in range(8):
        m, size = place_data(version, codewords, mask_id)
        p = penalty(m, size)
        if best is None or p < best[0]:
            best = (p, mask_id, m, size)
    return version, best[1], best[2], best[3]


# ------------------------------------------------------------------- SVG output
def to_svg(matrix, size, path, quiet=4, color="#2E1A0B"):
    scale = 1
    dim = size + quiet * 2
    parts = []
    for r in range(size):
        c = 0
        while c < size:
            if matrix[r][c]:
                start = c
                while c < size and matrix[r][c]:
                    c += 1
                parts.append("M%d %dh%dv1h-%dz" % (start + quiet, r + quiet, c - start, c - start))
            else:
                c += 1
    body = "".join(parts)
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" '
        'width="%d" height="%d" shape-rendering="crispEdges" role="img" '
        'aria-label="QR code linking to https://saadatalmuluk.com/">'
        '<rect width="%d" height="%d" fill="#FFFFFF"/>'
        '<path fill="%s" d="%s"/></svg>\n'
        % (dim, dim, dim * 8, dim * 8, dim, dim, color, body)
    )
    open(path, "w", encoding="utf-8", newline="\n").write(svg)
    return svg


# ------------------------------------------------------------------- SELF TEST
def self_test(text):
    version, mask_id, m, size = build(text)
    ec, blocks = ec_blocks(version)

    # --- 1. structural checks -------------------------------------------------
    assert size == version * 4 + 17
    for (br, bc) in ((0, 0), (0, size - 7), (size - 7, 0)):
        assert m[br + 3][bc + 3] == 1, "finder center"
        assert m[br][bc] == 1 and m[br + 6][bc + 6] == 1, "finder edge"
        assert m[br + 1][bc + 1] == 0, "finder ring"
    assert m[size - 8][8] == 1, "dark module"
    for i in range(8, size - 8):
        assert m[6][i] == (1 if i % 2 == 0 else 0), "h timing"
        assert m[i][6] == (1 if i % 2 == 0 else 0), "v timing"

    # --- 2. independently decode the module matrix ----------------------------
    dmask, base, _ = function_mask(version)
    # read format info from the matrix and confirm it round-trips
    fi = 0
    for i in range(15):
        if i < 6:
            bit = m[8][i]
        elif i == 6:
            bit = m[8][7]
        elif i == 7:
            bit = m[8][8]
        elif i == 8:
            bit = m[7][8]
        else:
            bit = m[14 - i][8]
        fi |= bit << i
    assert fi == format_info(mask_id), "format info mismatch in matrix"

    # walk the same zigzag the spec defines, un-mask, gather codewords
    bits = []
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for row in rows:
            for cc in (col, col - 1):
                if dmask[row][cc]:
                    continue
                bit = m[row][cc]
                if MASK_FUNCS[mask_id](row, cc):
                    bit ^= 1
                bits.append(bit)
        upward = not upward
        col -= 2
    cws = []
    for i in range(0, len(bits) - 7, 8):
        v = 0
        for b in bits[i:i + 8]:
            v = (v << 1) | b
        cws.append(v)

    # de-interleave
    total = total_codewords(version)
    cws = cws[:total]
    block_list = [[] for _ in blocks]
    idx = 0
    for i in range(max(blocks)):
        for bi, length in enumerate(blocks):
            if i < length:
                block_list[bi].append(cws[idx])
                idx += 1
    ec_list = [[] for _ in blocks]
    for _ in range(ec):
        for bi in range(len(blocks)):
            ec_list[bi].append(cws[idx])
            idx += 1

    # verify each block's Reed-Solomon remainder is (almost always) zero
    for bi, blk in enumerate(block_list):
        full = blk + ec_list[bi]
        # evaluate the codeword polynomial at the RS roots
        for i in range(ec):
            acc = 0
            for coef in full:
                acc = gf_mul(acc, EXP[i]) ^ coef
            assert acc == 0, "RS check failed block %d root %d" % (bi, i)

    payload = []
    for blk in block_list:
        payload.extend(blk)
    stream = []
    for cw in payload:
        for i in range(7, -1, -1):
            stream.append((cw >> i) & 1)

    def take(n):
        v = 0
        for b in stream[:n]:
            v = (v << 1) | b
        del stream[:n]
        return v

    mode = take(4)
    assert mode == 0b0100, "mode != byte (%s)" % bin(mode)
    length = take(8 if version <= 9 else 16)
    data = bytearray()
    for _ in range(length):
        data.append(take(8))
    decoded = data.decode("utf-8")
    assert decoded == text, "decoded %r != %r" % (decoded, text)

    return version, mask_id, total_codewords(version), decoded


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "qr-store.svg"
    url = "https://saadatalmuluk.com/"
    version, mask_id, _, decoded = self_test(url)
    print("self-test PASSED")
    print("  text      :", decoded)
    print("  version   : %d (%dx%d modules)" % (version, version * 4 + 17, version * 4 + 17))
    print("  mask      : %d" % mask_id)
    print("  ecc level : M")
    _, _, m, size = build(url)
    svg = to_svg(m, size, out)
    print("  wrote     : %s (%d bytes)" % (out, len(svg.encode("utf-8"))))
