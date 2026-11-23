"""Settings-Archive des Clients (GameData/Data/GameplaySettings/settings.bin,
settings2.bin): die GameplaySettings-JSONs, die der Client unter
"DB:>GameplaySettings" liest (GameSettingsArchive::Initialize, 0x1020040).

Datei = verschlüsselt( LZO1X(Inhalt) + u32 Inhaltsgröße ). Schlüssel-
strom (0x116ae60): Zustand s = 0xc2b2ae35; je Byte b ^= s & 0xff, dann
s = ((s >> 8) | Klartextbyte << 24) * 0xcc9e2d51. Entpacken: LZO1X
(0x1292f30). Inhalt: Ordnerbaum
  Name\\0, u32 Dateien, je Datei "PFAD\\0" + JSON + "\\0",
  u32 Unterordner, je Unterordner dasselbe;
am Ende u32 CRC32 des Inhalts davor.

Für Patches als kleines Delta (Launcher, xdelta): eine Änderung behält den
gepackten Anfang unverändert (Schlüsselstrom hängt nur vom Klartext davor
ab); ab der letzten passenden LZO-Anweisung folgt der Rest neu. replace_tail
hängt ihn als Literal-Block an (nur für Änderungen kurz vor dem Ende, z. B.
settings2.bin), replace_packed packt ihn mit lzo_compress (Änderungen mitten
in settings.bin: ab der Bling-Mine bei 22 % ~3,9 MB Delta statt 25 MB,
~5 s).
"""

import struct
import zlib

K0, K1 = 0xC2B2AE35, 0xCC9E2D51


def crypt(data, decrypt):
    out = bytearray(data)
    s = K0
    for i, b in enumerate(out):
        out[i] = b ^ (s & 0xFF)
        plain = out[i] if decrypt else b
        s = (((s >> 8) | plain << 24) * K1) & 0xFFFFFFFF
    return bytes(out)


def lzo_decompress(src):
    """LZO1X entpacken. Liefert (Inhalt, Schnittpunkte): Paare
    (Eingabeposition, Ausgabelänge), an denen eine neue Anweisung im
    Grundzustand beginnt (dort darf ein Literal-Block anschließen)."""
    out = bytearray()
    cuts = []
    ip = 0

    def run_length(t, base):
        nonlocal ip
        if t == 0:
            while src[ip] == 0:
                t += 255
                ip += 1
            t += base + src[ip]
            ip += 1
        return t

    def copy(m, n):
        for k in range(n):
            out.append(out[m + k])

    state = "loop"
    t = 0  # jeder Übergang nach "match" setzt t zuvor; Startwert nur für den Typprüfer
    if src[0] > 17:
        t = src[0] - 17
        ip = 1
        out += src[ip:ip + t]
        ip += t
        if t < 4:
            t = src[ip]
            ip += 1
            state = "match"
        else:
            state = "first"
    while True:
        if state == "loop":
            cuts.append((ip, len(out)))
            t = src[ip]
            ip += 1
            if t >= 16:
                state = "match"
                continue
            t = run_length(t, 15)
            out += src[ip:ip + t + 3]
            ip += t + 3
            state = "first"
        elif state == "first":
            t = src[ip]
            ip += 1
            if t >= 16:
                state = "match"
                continue
            m = len(out) - 0x801 - (t >> 2) - (src[ip] << 2)
            ip += 1
            copy(m, 3)
            state = "done"
        elif state == "match":
            if t >= 64:
                m = len(out) - 1 - ((t >> 2) & 7) - (src[ip] << 3)
                ip += 1
                n = (t >> 5) + 1
            elif t >= 32:
                t = run_length(t & 31, 31)
                m = len(out) - 1 - ((src[ip] | src[ip + 1] << 8) >> 2)
                ip += 2
                n = t + 2
            elif t >= 16:
                m = len(out) - ((t & 8) << 11)
                t = run_length(t & 7, 7)
                m -= (src[ip] | src[ip + 1] << 8) >> 2
                ip += 2
                if m == len(out):
                    return bytes(out), cuts
                m -= 0x4000
                n = t + 2
            else:
                m = len(out) - 1 - (t >> 2) - (src[ip] << 2)
                ip += 1
                n = 2
            copy(m, n)
            state = "done"
        else:  # done: 0-3 Literale hinter dem Match
            t = src[ip - 2] & 3
            if t == 0:
                state = "loop"
                continue
            out += src[ip:ip + t]
            ip += t
            t = src[ip]
            ip += 1
            state = "match"


def _run(n, mask):
    """Längenfeld einer Anweisung: n <= mask direkt im Befehlsbyte, sonst 0
    im Befehlsbyte, Null-Bytes zu je 255 und ein Schlussbyte (nicht 0)."""
    if n <= mask:
        return n, b""
    n -= mask
    zeros = (n - 1) // 255
    return 0, b"\x00" * zeros + bytes([n - 255 * zeros])


def lzo_compress(data, start=0):
    """LZO1X-Anweisungen für data[start:], Ende inklusive. Der Entpacker
    steht dabei im Grundzustand und hat data[:start] schon ausgegeben
    (Schnittpunkt aus lzo_decompress); Matches dürfen dorthin zurückgreifen.
    Greedy wie LZO1X-1: Hash über 4 Bytes, ein Kandidat je Hash, Positionen
    im Match werden nicht eingetragen. Benutzt nur M2/M3/M4 (Länge >= 3),
    kein M1, damit Literal-Runs >= 4 und 1-3 Literale hinter Matches
    reichen."""
    MAX_LEN = 2048
    out = bytearray()
    table = {}
    n = len(data)
    lit = start  # Beginn der noch nicht ausgegebenen Literale
    state_at = None  # Index im out für die 2 Bits "Literale danach"
    # Erst einmal 4 Literale: im Grundzustand ohne vorigen Match gehen nur
    # Literal-Runs >= 4
    for p in range(max(0, start - 0xBFFF), start):
        table[data[p:p + 4]] = p
    i = start + 4 if n - start > 4 else n

    def literals(end):
        nonlocal state_at
        k = end - lit
        if k == 0:
            return
        if state_at is not None and k <= 3:
            out[state_at] |= k
        elif k < 4:
            raise ValueError("Literal-Run braucht mindestens 4 Bytes")
        else:
            t, ext = _run(k - 3, 15)
            out.append(t)
            out.extend(ext)
        out.extend(data[lit:end])
        state_at = None

    limit = n - 4
    while i < limit:
        key = data[i:i + 4]
        cand = table.get(key)
        table[key] = i
        dist = i - cand if cand is not None else 0
        if cand is None or dist > 0xBFFF:
            i += 1
            continue
        # Match verlängern (die ersten 4 Bytes stimmen)
        m = 4
        top = min(MAX_LEN, n - i)
        while m < top and data[cand + m] == data[i + m]:
            m += 1
        literals(i)
        if m <= 8 and dist <= 0x800:  # M2
            d = dist - 1
            out.append(((m - 1) << 5) | ((d & 7) << 2))
            state_at = len(out) - 1
            out.append(d >> 3)
        elif dist <= 0x4000:  # M3
            t, ext = _run(m - 2, 31)
            out.append(32 | t)
            out += ext
            state_at = len(out)
            out += struct.pack("<H", (dist - 1) << 2)
        else:  # M4
            d = dist - 0x4000
            t, ext = _run(m - 2, 7)
            out.append(16 | ((d >> 11) & 8) | t)
            out += ext
            state_at = len(out)
            out += struct.pack("<H", (d & 0x3FFF) << 2)
        i += m
        lit = i
    literals(n)
    return bytes(out) + b"\x11\x00\x00"


def _literal_block(data):
    """LZO1X-Anweisungen: data als Literal-Block, dann Ende."""
    n = len(data) - 3  # Literal-Block im Grundzustand: t + 3 Bytes, t >= 1
    if n < 1:
        raise ValueError("Literal-Block braucht mindestens 4 Bytes")
    if n <= 15:
        head = bytes([n])
    else:  # t = 0: 15 + 255 je Null-Byte + Schlussbyte (nicht 0)
        zeros = (n - 16) // 255
        head = b"\x00" + b"\x00" * zeros + bytes([n - 15 - 255 * zeros])
    return head + data + b"\x11\x00\x00"


def read(blob):
    """(Inhalt, gepackter Strom, Schnittpunkte) einer Archivdatei."""
    plain = crypt(blob, decrypt=True)
    packed = plain[:-4]
    content, cuts = lzo_decompress(packed)
    if struct.unpack("<I", plain[-4:])[0] != len(content):
        raise ValueError("Settings-Archiv: Größe stimmt nicht")
    if struct.unpack("<I", content[-4:])[0] != zlib.crc32(content[:-4]):
        raise ValueError("Settings-Archiv: CRC stimmt nicht")
    return content, packed, cuts


def replace_tail(blob, offset, new_tail):
    """Archivdatei, deren Inhalt ab `offset` (vor der CRC) durch new_tail
    ersetzt ist; Anfang des gepackten Stroms bleibt Byte für Byte gleich."""
    content, packed, cuts = read(blob)
    body = content[:offset] + new_tail
    body += struct.pack("<I", zlib.crc32(body))
    ip, done = max((c for c in cuts if c[1] <= offset), key=lambda c: c[1])
    stream = packed[:ip] + _literal_block(body[done:])
    return crypt(stream + struct.pack("<I", len(body)), decrypt=False)


def replace_packed(blob, offset, new_tail):
    """Wie replace_tail, der Rest aber mit lzo_compress gepackt."""
    content, packed, cuts = read(blob)
    body = content[:offset] + new_tail
    body += struct.pack("<I", zlib.crc32(body))
    ip, done = max((c for c in cuts if c[1] <= offset), key=lambda c: c[1])
    stream = packed[:ip] + lzo_compress(body, done)
    return crypt(stream + struct.pack("<I", len(body)), decrypt=False)