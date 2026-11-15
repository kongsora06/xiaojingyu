"""Chat lines cut the way people text: one sentence per message.

A line is cut after 。！？ (and after ! or ? when a space or a Chinese character follows, so
links and code are left alone), keeping closing quotes and brackets with the sentence.
A piece with no words in it, like a kaomoji or "～", stays with the sentence before it
(or, at the start of a line, with the one after it).
Long lines are prose (a story, a letter) and stay whole.
"""

import re

# Past this, a line is prose rather than chat and is not cut.
PROSE_CHARS = 150
END = re.compile(r"(?:[。！？]+|[!?]+(?=\s|[^\x00-\x7f]))[”’」』）)\]】~～]*")
# Words: Chinese, kana or plain letters and digits; kaomoji use other letter-like symbols.
WORD = re.compile(r"[一-鿿㐀-䶿぀-ヿA-Za-z0-9]")


def sentences(line: str) -> list[str]:
    line = line.strip()
    if not line:
        return []
    if len(line) > PROSE_CHARS:
        return [line]
    pieces, start = [], 0
    for match in END.finditer(line):
        pieces.append(line[start : match.end()])
        start = match.end()
    pieces.append(line[start:])
    found: list[str] = []
    lead = ""  # A word-less start ("？", "……") goes with the sentence after it.
    for piece in (piece.strip() for piece in pieces):
        if not piece:
            continue
        if WORD.search(piece):
            found.append(lead + piece)
            lead = ""
        elif found:
            found[-1] += piece
        else:
            lead += piece
    return found or [line]


def chat_lines(text: str) -> list[str]:
    """Every line of the text, each cut into sentences."""
    return [piece for line in text.split("\n") for piece in sentences(line)]
