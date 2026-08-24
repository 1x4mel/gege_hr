"""VietQR EMVCo encoder — Python port of the SPA ``src/utils/vietqr.js``.

Spec: https://vietqr.net/portal-service/download/documents/
VietQR-Service-User-Guide-v1.0-202106.pdf

Pure / bench-free (mirrors utils/calc.py + utils/advance.py conventions) so
the CRC + TLV maths are unit-testable outside a Frappe site. The JS encoder
remains the source for client-side previews; both MUST produce byte-identical
strings — ``tests/test_vietqr.py`` pins the shared vector.
"""

from __future__ import annotations

import unicodedata

_VIETQR_GUID = "A000000727"
_SERVICE_TRANSFER = "QRIBFTTA"

# CRC16-CCITT-FALSE lookup table (identical to the JS CRC_TABLE).
_CRC_TABLE = [
    0,
    4129,
    8258,
    12387,
    16516,
    20645,
    24774,
    28903,
    33032,
    37161,
    41290,
    45419,
    49548,
    53677,
    57806,
    61935,
    4657,
    528,
    12915,
    8786,
    21173,
    17044,
    29431,
    25302,
    37689,
    33560,
    45947,
    41818,
    54205,
    50076,
    62463,
    58334,
    9314,
    13379,
    1056,
    5121,
    25830,
    29895,
    17572,
    21637,
    42346,
    46411,
    34088,
    38153,
    58862,
    62927,
    50604,
    54669,
    13907,
    9842,
    5649,
    1584,
    30423,
    26358,
    22165,
    18100,
    46939,
    42874,
    38681,
    34616,
    63455,
    59390,
    55197,
    51132,
    18628,
    22757,
    26758,
    30887,
    2112,
    6241,
    10242,
    14371,
    51660,
    55789,
    59790,
    63919,
    35144,
    39273,
    43274,
    47403,
    23285,
    19156,
    31415,
    27286,
    6769,
    2640,
    14899,
    10770,
    56317,
    52188,
    64447,
    60318,
    39801,
    35672,
    47931,
    43802,
    27814,
    31879,
    19684,
    23749,
    11298,
    15363,
    3168,
    7233,
    60846,
    64911,
    52716,
    56781,
    44330,
    48395,
    36200,
    40265,
    32407,
    28342,
    24277,
    20212,
    15891,
    11826,
    7761,
    3696,
    65439,
    61374,
    57309,
    53244,
    48923,
    44858,
    40793,
    36728,
    37256,
    33193,
    45514,
    41451,
    53516,
    49453,
    61774,
    57711,
    4224,
    161,
    12482,
    8419,
    20484,
    16421,
    28742,
    24679,
    33721,
    37784,
    41979,
    46042,
    49981,
    54044,
    58239,
    62302,
    689,
    4752,
    8947,
    13010,
    16949,
    21012,
    25207,
    29270,
    46570,
    42443,
    38312,
    34185,
    62830,
    58703,
    54572,
    50445,
    13538,
    9411,
    5280,
    1153,
    29798,
    25671,
    21540,
    17413,
    42971,
    47098,
    34713,
    38840,
    59231,
    63358,
    50973,
    55100,
    9939,
    14066,
    1681,
    5808,
    26199,
    30326,
    17941,
    22068,
    55628,
    51565,
    63758,
    59695,
    39368,
    35305,
    47498,
    43435,
    22596,
    18533,
    30726,
    26663,
    6336,
    2273,
    14466,
    10403,
    52093,
    56156,
    60223,
    64286,
    35833,
    39896,
    43963,
    48026,
    19061,
    23124,
    27191,
    31254,
    2801,
    6864,
    10931,
    14994,
    64814,
    60687,
    56684,
    52557,
    48554,
    44427,
    40424,
    36297,
    31782,
    27655,
    23652,
    19525,
    15522,
    11395,
    7392,
    3265,
    61215,
    65342,
    53085,
    57212,
    44955,
    49082,
    36825,
    40952,
    28183,
    32310,
    20053,
    24180,
    11923,
    16050,
    3793,
    7920,
]


def crc16_ccitt_false(data: str) -> int:
    crc = 0xFFFF
    for ch in data:
        crc = (_CRC_TABLE[((crc >> 8) ^ ord(ch)) & 0xFF] ^ (crc << 8)) & 0xFFFF
    return crc


def crc_string(data: str) -> str:
    return ("0000" + format(crc16_ccitt_false(data), "X"))[-4:]


def _f(tag: str, value) -> str:
    """TLV (Tag-Length-Value) — 2-digit tag + 2-digit length + value."""
    if not tag or len(tag) != 2 or value is None or value == "":
        return ""
    val = str(value)
    return tag + ("00" + str(len(val)))[-2:] + val


def remove_accents(text: str) -> str:
    """ASCII-uppercase memo text (VietQR requirement) — port of the JS util."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFD", str(text))
    stripped = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    stripped = stripped.replace("đ", "d").replace("Đ", "D")
    out = []
    for c in stripped.upper():
        if ("A" <= c <= "Z") or ("0" <= c <= "9") or c == " ":
            out.append(c)
    return "".join(out).strip()


def encode_vietqr(bank_bin: str, account_number: str, amount=None, description: str = "") -> str:
    """Build the EMVCo/NAPAS VietQR payload string (with CRC)."""
    bin_ = str(bank_bin or "").strip()
    acc = str(account_number or "").strip()

    tag_00 = _f("00", "01")
    tag_01 = _f("01", "12" if amount else "11")

    guid = _f("00", _VIETQR_GUID)
    provider_data = _f("00", bin_) + _f("01", acc)
    service = _f("02", _SERVICE_TRANSFER)
    tag_38 = _f("38", guid + _f("01", provider_data) + service)

    tag_53 = _f("53", "704")  # VND
    tag_54 = _f("54", str(int(amount))) if amount else ""
    tag_58 = _f("58", "VN")

    purpose = _f("08", remove_accents(description)) if description else ""
    tag_62 = _f("62", purpose) if purpose else ""

    payload = tag_00 + tag_01 + tag_38 + tag_53 + tag_54 + tag_58 + tag_62 + "6304"
    return payload + crc_string(payload)


def verify_vietqr(qr_text: str) -> bool:
    """True when the trailing CRC matches the payload (integrity check)."""
    if not qr_text or len(qr_text) < 8:
        return False
    return crc_string(qr_text[:-4]) == qr_text[-4:].upper()
