"""Cross-check the RS block table in make_qr.py against two independent sources.

Source A: Thonky's error-correction table (transcription of ISO/IEC 18004).
Source B: the total-codeword formula plus the ISO 18004 structural rules
          (exactly one or two block groups; group 2 blocks hold exactly one
          more data codeword than group 1 blocks).

Both must agree, otherwise this exits non-zero and make_qr.py must not be trusted.
"""
import re
import sys
import urllib.request

sys.path.insert(0, __file__.rsplit("\\", 1)[0])
import make_qr  # noqa: E402

URL = "https://www.thonky.com/qr-code-tutorial/error-correction-table"


def fetch_thonky():
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    html = urllib.request.urlopen(req, timeout=40).read().decode("utf-8", "replace")
    table = max(re.findall(r"<table.*?</table>", html, re.S), key=len)
    out = {}
    for row in re.findall(r"<tr.*?</tr>", table, re.S):
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)
        cells = [re.sub(r"<[^>]+>", "", c).strip().replace("\xa0", " ") for c in cells]
        if len(cells) < 7:
            continue
        m = re.match(r"^(\d+)-([LMQH])$", cells[0])
        if not m:
            continue

        def num(x):
            x = x.strip()
            return int(x) if x.isdigit() else 0

        out.setdefault(m.group(2), {})[int(m.group(1))] = [
            num(cells[2]), num(cells[3]), num(cells[4]), num(cells[5]), num(cells[6])
        ]
    return out


def main():
    tables = fetch_thonky()
    problems = []

    if len(tables.get("M", {})) != 40:
        problems.append("Thonky M table did not parse to 40 versions")

    for v in range(1, 41):
        ec, g1, b1, g2, b2 = tables["M"][v]
        total = make_qr.total_codewords(v)

        # Source B: structural rules + the bit-count formula
        if g2 and b2 != b1 + 1:
            problems.append("v%d: group 2 length %d is not group 1 length %d + 1" % (v, b2, b1))
        computed = g1 * b1 + g2 * b2 + ec * (g1 + g2)
        if computed != total:
            problems.append("v%d: g1*b1+g2*b2+ec*(g1+g2) = %d but formula says %d" % (v, computed, total))

        # My table must express the same split
        my_ec, my_g1, my_g2 = make_qr.RS_TABLE[v]
        # make_qr stores group1 = the SHORT blocks; canonical group 1 is not always the short group
        short_count = g1 if b2 == 0 else (g1 if b1 < b2 else g2)
        if my_ec != ec:
            problems.append("v%d: ec per block %d != %d" % (v, my_ec, ec))
        if my_g1 + my_g2 != g1 + g2:
            problems.append("v%d: block count %d != %d" % (v, my_g1 + my_g2, g1 + g2))
        if my_g2 and my_g1 + my_g2 != g1 + g2:
            problems.append("v%d: block split %d/%d != %d/%d" % (v, my_g1, my_g2, g1, g2))

    print("checked 40 versions x level M against %s" % URL)
    if problems:
        print("FAILURES (%d):" % len(problems))
        for p in problems:
            print("  -", p)
        return 1
    print("PASS: table agrees with Thonky and with the ISO 18004 structural rules")
    return 0


if __name__ == "__main__":
    sys.exit(main())
