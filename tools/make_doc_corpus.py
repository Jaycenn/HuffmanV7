#!/usr/bin/env python3
"""
tools/make_doc_corpus.py — deterministic PDF and DOCX test corpus.

Generates the document matrix the V7 brief asks to be tested (§40): text-heavy
and multi-page PDFs, PDFs with images, PDFs with already-compressed streams,
small and large ones, and the DOCX equivalents including tables, images,
styles and multiple XML parts.

NOTE ON zlib / zipfile IN THIS FILE
-----------------------------------
This is a CORPUS GENERATOR. It uses zlib and zipfile to *manufacture realistic
input files* — a real DOCX is a ZIP of deflated parts, and a real Word-exported
PDF has FlateDecode content streams, so producing authentic test inputs
requires them.

Nothing here is part of the compressor. The engine (afc.py, afc2.py,
afc_native.cpp), the container-aware layer (containers.py) and the archive
format (afcpak.py) import no compression library at all, and tests assert that
by parsing their ASTs. Generating a .docx with zipfile is exactly as neutral as
downloading one from the internet would be.

Everything is seeded, so the corpus is byte-reproducible across machines.
"""
import os
import random
import sys
import zlib
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "benchmarks", "documents")

LOREM = (
    "Adaptive File Compression using multi-level frequency analysis and a "
    "hybrid Huffman coder. The engine builds a per-file dictionary of frequent "
    "patterns through a three-tier scan, admitting each pattern only when the "
    "Bit Cost Decision Engine proves it pays for itself, growing accepted "
    "patterns into larger structural blocks, auditing the dictionary against "
    "actual counts, and coding the resulting mixed symbol stream with one "
    "canonical hybrid Huffman tree. ")


def rng(seed):
    return random.Random(seed)


def paragraphs(n, seed):
    r = rng(seed)
    out = []
    words = LOREM.split()
    for i in range(n):
        k = r.randint(28, 70)
        start = r.randint(0, max(0, len(words) - 8))
        text = " ".join((words * 6)[start:start + k])
        out.append("Section %d. %s" % (i + 1, text))
    return out


# ---------------------------------------------------------------------------
# fake but structurally valid binary payloads
# ---------------------------------------------------------------------------

def fake_jpeg(nbytes, seed):
    """JPEG header + incompressible body — stands in for a real photo."""
    r = rng(seed)
    head = bytes([0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10]) + b"JFIF\x00\x01\x01\x00"
    body = bytes(r.getrandbits(8) for _ in range(max(0, nbytes - len(head) - 2)))
    return head + body + bytes([0xFF, 0xD9])


def fake_png(nbytes, seed):
    r = rng(seed)
    head = b"\x89PNG\r\n\x1a\n"
    body = bytes(r.getrandbits(8) for _ in range(max(0, nbytes - len(head))))
    return head + body


# ---------------------------------------------------------------------------
# PDF construction
# ---------------------------------------------------------------------------

def build_pdf(path, pages, flate_streams, images, seed, image_bytes=40000):
    """Assemble a valid PDF with a real xref table.

    `flate_streams` selects Word-style FlateDecode content streams; otherwise
    the content streams are stored uncompressed, which many generators emit.
    """
    objects = {}
    nobj = 0

    def add(body):
        nonlocal nobj
        nobj += 1
        objects[nobj] = body
        return nobj

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    img_ids = []
    for k in range(images):
        raw = fake_jpeg(image_bytes, seed + 900 + k)
        img = add(b"<< /Type /XObject /Subtype /Image /Width 320 /Height 240 "
                  b"/ColorSpace /DeviceRGB /BitsPerComponent 8 "
                  b"/Filter /DCTDecode /Length " + str(len(raw)).encode()
                  + b" >>\nstream\n" + raw + b"\nendstream")
        img_ids.append(img)

    page_ids = []
    content_ids = []
    paras = paragraphs(pages * 6, seed)
    for p in range(pages):
        lines = []
        lines.append(b"BT /F1 11 Tf 60 760 Td 14 TL")
        for i in range(6):
            txt = paras[p * 6 + i].replace("(", "").replace(")", "")
            lines.append(b"(" + txt.encode("latin-1", "replace") + b") Tj T*")
        lines.append(b"ET")
        content = b"\n".join(lines)
        if flate_streams:
            payload = zlib.compress(content, 6)
            dic = (b"<< /Filter /FlateDecode /Length "
                   + str(len(payload)).encode() + b" >>")
        else:
            payload = content
            dic = b"<< /Length " + str(len(payload)).encode() + b" >>"
        cid = add(dic + b"\nstream\n" + payload + b"\nendstream")
        content_ids.append(cid)

    pages_id = nobj + pages + 1
    for p in range(pages):
        res = b"<< /Font << /F1 " + str(font).encode() + b" 0 R >>"
        if img_ids:
            res += b" /XObject << /Im0 " + str(img_ids[p % len(img_ids)]).encode() \
                   + b" 0 R >>"
        res += b" >>"
        page_ids.append(add(
            b"<< /Type /Page /Parent " + str(pages_id).encode()
            + b" 0 R /MediaBox [0 0 612 792] /Resources " + res
            + b" /Contents " + str(content_ids[p]).encode() + b" 0 R >>"))

    kids = b" ".join(str(i).encode() + b" 0 R" for i in page_ids)
    pages_obj = add(b"<< /Type /Pages /Count " + str(pages).encode()
                    + b" /Kids [" + kids + b"] >>")
    root = add(b"<< /Type /Catalog /Pages " + str(pages_obj).encode() + b" 0 R >>")
    info = add(b"<< /Title (AFC V7 test document) /Producer (afc-corpus) "
               b"/CreationDate (D:20260101000000Z) >>")

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for num in sorted(objects):
        offsets[num] = len(out)
        out += str(num).encode() + b" 0 obj\n" + objects[num] + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 " + str(nobj + 1).encode() + b"\n"
    out += b"0000000000 65535 f \n"
    for num in sorted(objects):
        out += ("%010d 00000 n \n" % offsets[num]).encode()
    out += (b"trailer\n<< /Size " + str(nobj + 1).encode()
            + b" /Root " + str(root).encode() + b" 0 R /Info "
            + str(info).encode() + b" 0 R >>\nstartxref\n"
            + str(xref_at).encode() + b"\n%%EOF\n")
    open(path, "wb").write(bytes(out))
    return len(out)


# ---------------------------------------------------------------------------
# DOCX construction
# ---------------------------------------------------------------------------

def docx_document_xml(paras, tables=0):
    body = []
    for p in paras:
        body.append(
            '<w:p><w:pPr><w:pStyle w:val="BodyText"/><w:spacing w:after="120"/>'
            '</w:pPr><w:r><w:rPr><w:rFonts w:ascii="Calibri" '
            'w:hAnsi="Calibri"/><w:sz w:val="22"/></w:rPr>'
            '<w:t xml:space="preserve">%s</w:t></w:r></w:p>' % p)
    for t in range(tables):
        rows = []
        for r in range(6):
            cells = []
            for c in range(4):
                cells.append(
                    '<w:tc><w:tcPr><w:tcW w:w="2400" w:type="dxa"/></w:tcPr>'
                    '<w:p><w:r><w:t>Row %d Col %d value %d</w:t></w:r></w:p>'
                    '</w:tc>' % (r, c, r * 4 + c))
            rows.append('<w:tr>%s</w:tr>' % "".join(cells))
        body.append('<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/>'
                    '</w:tblPr>%s</w:tbl>' % "".join(rows))
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/'
            'wordprocessingml/2006/main"><w:body>%s'
            '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/></w:sectPr>'
            '</w:body></w:document>' % "".join(body))


_STYLES = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
           '<w:styles xmlns:w="http://schemas.openxmlformats.org/'
           'wordprocessingml/2006/main">'
           + "".join('<w:style w:type="paragraph" w:styleId="S%d">'
                     '<w:name w:val="Style %d"/><w:pPr><w:spacing '
                     'w:after="%d"/></w:pPr><w:rPr><w:sz w:val="%d"/>'
                     '</w:rPr></w:style>' % (i, i, 100 + i, 20 + (i % 8))
                     for i in range(60))
           + '</w:styles>')

_NUMBERING = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
              '<w:numbering xmlns:w="http://schemas.openxmlformats.org/'
              'wordprocessingml/2006/main">'
              + "".join('<w:abstractNum w:abstractNumId="%d"><w:lvl '
                        'w:ilvl="0"><w:numFmt w:val="decimal"/><w:lvlText '
                        'w:val="%%1."/></w:lvl></w:abstractNum>' % i
                        for i in range(20))
              + '</w:numbering>')

_SETTINGS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
             '<w:settings xmlns:w="http://schemas.openxmlformats.org/'
             'wordprocessingml/2006/main"><w:zoom w:percent="100"/>'
             '<w:defaultTabStop w:val="720"/></w:settings>')

_THEME = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
          '<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/'
          '2006/main" name="Office">'
          + "".join('<a:srgbClr val="%06X"/>' % (i * 7919 % 0xFFFFFF)
                    for i in range(120))
          + '</a:theme>')


def build_docx(path, paras, tables=0, images=0, seed=1, image_bytes=40000,
               compresslevel=6):
    ct = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
          'content-types"><Default Extension="rels" ContentType="application/'
          'vnd.openxmlformats-package.relationships+xml"/><Default '
          'Extension="xml" ContentType="application/xml"/><Default '
          'Extension="jpeg" ContentType="image/jpeg"/><Override '
          'PartName="/word/document.xml" ContentType="application/'
          'vnd.openxmlformats-officedocument.wordprocessingml.document.main'
          '+xml"/></Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/'
            '2006/relationships"><Relationship Id="rId1" Type="http://'
            'schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'officeDocument" Target="word/document.xml"/></Relationships>')
    drels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
             '<Relationships xmlns="http://schemas.openxmlformats.org/package/'
             '2006/relationships">'
             + "".join('<Relationship Id="rId%d" Type="http://schemas.'
                       'openxmlformats.org/officeDocument/2006/relationships/'
                       'image" Target="media/image%d.jpeg"/>' % (i + 10, i)
                       for i in range(max(1, images)))
             + '</Relationships>')

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=compresslevel) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", docx_document_xml(paras, tables))
        z.writestr("word/styles.xml", _STYLES)
        z.writestr("word/numbering.xml", _NUMBERING)
        z.writestr("word/settings.xml", _SETTINGS)
        z.writestr("word/theme/theme1.xml", _THEME)
        z.writestr("word/_rels/document.xml.rels", drels)
        for i in range(images):
            # images are stored, as Word does for already-compressed media
            zi = zipfile.ZipInfo("word/media/image%d.jpeg" % i)
            zi.compress_type = zipfile.ZIP_STORED
            z.writestr(zi, fake_jpeg(image_bytes, seed + 500 + i))
    return os.path.getsize(path)


def main():
    os.makedirs(OUT, exist_ok=True)
    made = []

    # ---- PDFs -------------------------------------------------------------
    made.append(("pdf_text_small.pdf",
                 build_pdf(os.path.join(OUT, "pdf_text_small.pdf"),
                           pages=2, flate_streams=False, images=0, seed=1)))
    made.append(("pdf_text_multipage.pdf",
                 build_pdf(os.path.join(OUT, "pdf_text_multipage.pdf"),
                           pages=40, flate_streams=False, images=0, seed=2)))
    made.append(("pdf_text_large.pdf",
                 build_pdf(os.path.join(OUT, "pdf_text_large.pdf"),
                           pages=160, flate_streams=False, images=0, seed=3)))
    made.append(("pdf_word_flate.pdf",
                 build_pdf(os.path.join(OUT, "pdf_word_flate.pdf"),
                           pages=40, flate_streams=True, images=0, seed=4)))
    made.append(("pdf_images_only.pdf",
                 build_pdf(os.path.join(OUT, "pdf_images_only.pdf"),
                           pages=6, flate_streams=False, images=6, seed=5)))
    made.append(("pdf_text_and_images.pdf",
                 build_pdf(os.path.join(OUT, "pdf_text_and_images.pdf"),
                           pages=30, flate_streams=False, images=4, seed=6)))
    made.append(("pdf_flate_and_images.pdf",
                 build_pdf(os.path.join(OUT, "pdf_flate_and_images.pdf"),
                           pages=30, flate_streams=True, images=4, seed=7)))

    # ---- DOCX -------------------------------------------------------------
    made.append(("docx_text_small.docx",
                 build_docx(os.path.join(OUT, "docx_text_small.docx"),
                            paragraphs(12, 11), seed=11)))
    made.append(("docx_text_multipage.docx",
                 build_docx(os.path.join(OUT, "docx_text_multipage.docx"),
                            paragraphs(300, 12), seed=12)))
    made.append(("docx_text_large.docx",
                 build_docx(os.path.join(OUT, "docx_text_large.docx"),
                            paragraphs(1500, 13), seed=13)))
    made.append(("docx_tables.docx",
                 build_docx(os.path.join(OUT, "docx_tables.docx"),
                            paragraphs(120, 14), tables=25, seed=14)))
    made.append(("docx_images.docx",
                 build_docx(os.path.join(OUT, "docx_images.docx"),
                            paragraphs(60, 15), images=5, seed=15)))
    made.append(("docx_text_and_images.docx",
                 build_docx(os.path.join(OUT, "docx_text_and_images.docx"),
                            paragraphs(400, 16), tables=8, images=3, seed=16)))
    made.append(("docx_stored.docx",
                 build_docx(os.path.join(OUT, "docx_stored.docx"),
                            paragraphs(300, 17), seed=17, compresslevel=0)))

    print("wrote %d documents to %s\n" % (len(made), OUT))
    for name, size in made:
        print("  %-28s %10d bytes" % (name, size))
    return 0


if __name__ == "__main__":
    sys.exit(main())
