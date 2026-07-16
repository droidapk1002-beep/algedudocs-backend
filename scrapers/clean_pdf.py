import re
import zlib
import io
import logging

logger = logging.getLogger(__name__)

URL_RX = re.compile(
    r'(?:https?://(?:www\.)?|www\.)?'
    r'(?:dzexams\.com|[a-z0-9-]+\.ency-education\.com|ency-education\.com|eddirasa\.com)\s*',
    re.IGNORECASE,
)
WATERMARK_JOINED_RX = re.compile(
    r'ency[- ]?education|dzexams|eddirasa|moutamadris', re.IGNORECASE
)
FOOTER_RX = re.compile(
    r'T[eéèê]l[eéèê]charger d[\' \s]*autres\s+(examens|sujets)\s+sur\s*', re.IGNORECASE
)
EMAIL_RX = re.compile(
    r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\s*', re.IGNORECASE
)

FOOTER_COVER_HEIGHT = 20


def has_watermark_pattern(text):
    if not re.search(r'BT[\s\S]*?Tm', text, re.IGNORECASE):
        return False
    blocks = re.findall(r'BT[\s\S]*?ET', text, re.IGNORECASE)
    if not blocks:
        return False
    tm6 = re.compile(
        r'(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\s+'
        r'(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\s+Tm',
        re.IGNORECASE,
    )
    for block in blocks:
        m = tm6.search(block)
        if not m:
            continue
        b = float(m.group(2))
        c = float(m.group(3))
        if abs(b) > 5 and abs(c) > 5:
            return True
    return False


def extract_tj_texts(text):
    out = []
    for t in re.finditer(r'\(([^)]*)\)\s*Tj', text, re.IGNORECASE):
        out.append(t.group(1))
    for arr in re.finditer(r'\[([^\]]*)\]\s*TJ', text, re.IGNORECASE):
        for t in re.finditer(r'\(([^)]*)\)', arr.group(1)):
            out.append(t.group(1))
    return out


def decode_hex_content(hex_str):
    try:
        raw_bytes = bytes.fromhex(hex_str.replace(' ', ''))
    except ValueError:
        return None
    raw = raw_bytes.decode('latin-1', errors='replace')
    if re.search(r'dzexams\.com|ency-education\.com|eddirasa\.com|@', raw, re.IGNORECASE):
        return raw
    if len(raw_bytes) >= 4 and all(b == 0 for b in raw_bytes[0::2]):
        try:
            raw = raw_bytes.decode('utf-16be')
        except UnicodeDecodeError:
            return None
        if re.search(r'dzexams\.com|ency-education\.com|eddirasa\.com|@', raw, re.IGNORECASE):
            return raw
    return None


def has_any_target(raw):
    if re.search(r'dzexams\.com|ency-education\.com|eddirasa\.com|@', raw, re.IGNORECASE):
        return True
    if has_watermark_pattern(raw):
        return True
    # Check hex strings in content streams (e.g. [<7777772e...>]TJ)
    for m in re.finditer(r'<([0-9A-Fa-f\s]{4,})>', raw):
        if decode_hex_content(m.group(1)) is not None:
            return True
    joined = ''.join(extract_tj_texts(raw)).replace(' ', '').lower()
    if WATERMARK_JOINED_RX.search(joined):
        return True
    return False


def clean_text(raw):
    return FOOTER_RX.sub('', URL_RX.sub('', EMAIL_RX.sub('', raw))).strip()


def decode_and_clean(hex_str):
    try:
        raw_bytes = bytes.fromhex(hex_str.replace(' ', ''))
    except ValueError:
        return None
    raw = raw_bytes.decode('latin-1', errors='replace')
    if not re.search(r'dzexams\.com|ency-education\.com|eddirasa\.com|@', raw, re.IGNORECASE):
        # Try UTF-16BE (alternating null bytes, common in CIDFont PDFs)
        if len(raw_bytes) >= 4 and all(b == 0 for b in raw_bytes[0::2]):
            try:
                raw = raw_bytes.decode('utf-16be')
            except UnicodeDecodeError:
                return None
        else:
            return None
    cleaned = clean_text(raw)
    if cleaned == raw:
        return None
    if len(cleaned) == 0:
        return '<>'
    if len(raw_bytes) >= 2 and all(b == 0 for b in raw_bytes[0::2]):
        return '<' + cleaned.encode('utf-16be').hex() + '>'
    return '<' + cleaned.encode('latin-1').hex() + '>'


def remove_urls_from_content(text):
    r = clean_text(text)

    def replace_hex(m):
        hex_content = m.group(1).replace(' ', '')
        if len(hex_content) % 2 != 0:
            return m.group(0)
        cleaned = decode_and_clean(hex_content)
        return cleaned if cleaned is not None else m.group(0)

    r = re.sub(r'<([0-9A-Fa-f\s]+)>', replace_hex, r)

    def replace_artifact(m):
        match_text = m.group(0)
        if re.search(r'/Fm[01]\s+Do', match_text):
            return ''
        texts = extract_tj_texts(match_text)
        joined = ''.join(texts).replace(' ', '').lower()
        if len(joined) >= 3 and WATERMARK_JOINED_RX.search(joined):
            return ''
        return match_text

    r = re.sub(r'/Artifact[\s\S]*?EMC\s*Q?\s*', replace_artifact, r, flags=re.IGNORECASE)

    tm6 = re.compile(
        r'(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\s+'
        r'(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\s+Tm',
        re.IGNORECASE,
    )

    def replace_rotated(m):
        match_text = m.group(0)
        m2 = tm6.search(match_text)
        if not m2:
            return match_text
        b = float(m2.group(2))
        c = float(m2.group(3))
        if abs(b) < 5 and abs(c) < 5:
            return match_text
        texts = extract_tj_texts(match_text)
        total_len = sum(len(t) for t in texts)
        if total_len > 0:
            return ''
        return match_text

    r = re.sub(r'BT[\s\S]*?ET', replace_rotated, r, flags=re.IGNORECASE)

    def replace_watermark_bt(m):
        match_text = m.group(0)
        texts = extract_tj_texts(match_text)
        joined = ''.join(texts).replace(' ', '').lower()
        if len(joined) < 3:
            return match_text
        if WATERMARK_JOINED_RX.search(joined):
            return ''
        return match_text

    r = re.sub(r'BT[\s\S]*?ET', replace_watermark_bt, r, flags=re.IGNORECASE)

    return r


def is_scanned_page(content_text):
    trimmed = content_text.strip()
    return bool(
        re.match(
            r'^q\s+[\d.]+\s+0\s+0\s+[\d.]+\s+[\d.]+\s+[\d.]+\s+cm\s+/\w+\s+Do\s*Q\s*$',
            trimmed,
            re.IGNORECASE,
        )
    )


def apply_footer_cover(decoded_text, page_width):
    if not is_scanned_page(decoded_text):
        return None
    cover = 'q 1 g 0 0 {} {} re f Q\n'.format(page_width, FOOTER_COVER_HEIGHT)
    modified = re.sub(r'Q\s*$', 'Q\n' + cover, decoded_text, flags=re.IGNORECASE)
    if modified == decoded_text:
        return None
    return modified


def clean_stream_text(decoded_text, cover_footer=False, page_width=None):
    modified = decoded_text
    cleaned = False

    if has_any_target(modified):
        m = remove_urls_from_content(modified)
        if m != modified:
            modified = m
            cleaned = True

    if cover_footer and page_width is not None:
        covered = apply_footer_cover(modified, page_width)
        if covered is not None:
            modified = covered
            cleaned = True

    return modified if cleaned else None


def raw_buffer_clean(pdf_bytes, cover_footer=False):
    before = len(pdf_bytes)
    text = pdf_bytes.decode('latin-1', errors='replace')
    result = text

    stream_count = 0
    modified_count = 0

    def replace_stream(m):
        nonlocal modified_count
        data = m.group(1)
        raw = data.encode('latin-1')
        try:
            dec = zlib.decompress(raw)
        except zlib.error:
            return m.group(0)
        dec_text = dec.decode('latin-1', errors='replace')

        width = None
        if cover_footer:
            wm = re.search(r'/(\w+)\s+Do\s*Q\s*$', dec_text.strip(), re.IGNORECASE)
            if wm:
                mm = re.search(r'(\d+\.?\d*)\s+0\s+0\s+(\d+\.?\d*)', dec_text)
                if mm:
                    width = float(mm.group(1))

        cleaned = clean_stream_text(dec_text, cover_footer=cover_footer, page_width=width)
        if cleaned is None:
            return m.group(0)
        recomp = zlib.compress(cleaned.encode('latin-1'))
        modified_count += 1
        return 'stream\n' + recomp.decode('latin-1') + '\nendstream'

    result = re.sub(
        r'stream\n([\s\S]*?)\nendstream', replace_stream, result, flags=re.IGNORECASE
    )

    result = FOOTER_RX.sub('', URL_RX.sub('', EMAIL_RX.sub('', result)))

    if result == text:
        return None

    out = result.encode('latin-1', errors='replace')
    logger.info('Brut: %s %d flux dont %d modifiés, %d→%d octets',
                '✓' if modified_count else '‐', stream_count, modified_count, before, len(out))
    return out


def clean_pdf_links(pdf_bytes, clean=True, cover_footer=False):
    if not clean and not cover_footer:
        return pdf_bytes

    before = len(pdf_bytes)

    out = raw_buffer_clean(pdf_bytes, cover_footer=cover_footer)

    if out is None:
        logger.info('Aucune modification nécessaire, retour original')
        return pdf_bytes

    if len(out) < 100 or len(out) < before * 0.1:
        logger.warning('PDF invalide après nettoyage (%d octets), garde original', len(out))
        return pdf_bytes

    logger.info('Nettoyage OK: %d → %d octets (%.1f%% réduit)',
                before, len(out), (1 - len(out) / before) * 100)
    return out


def clean_pdf_file(filepath, clean=True, cover_footer=False):
    from pathlib import Path
    path = Path(filepath) if isinstance(filepath, str) else filepath
    before = path.stat().st_size
    data = path.read_bytes()
    out = clean_pdf_links(data, clean=clean, cover_footer=cover_footer)
    if out is not data:
        path.write_bytes(out)
        logger.info('Fichier %s nettoyé: %d → %d octets', path.name, before, len(out))
        return True
    return False
