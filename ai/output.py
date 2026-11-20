"""Jawaban lisan yang singkat; data detektor lengkap tetap ada di metadata API."""
import os
import re


def spoken_text(text, max_words=None):
    """Batasi jawaban lisan pada batas kata (default 100, env SPOKEN_MAX_WORDS)."""
    limit = max(10, int(max_words or os.getenv('SPOKEN_MAX_WORDS', '100')))
    words = str(text or '').split()
    if len(words) <= limit:
        return str(text or '').strip()
    excerpt = ' '.join(words[:limit]).rstrip('.,;:')
    return excerpt + '. Masih ada lanjutan.'


def concise_answer(text, max_words=70):
    """Buang komentar skor/confidence buatan model dari jawaban."""
    text = str(text or '').strip()
    text = re.sub(r'(?i)\s*\(?\b(?:dengan\s+)?(?:confidence|probability|probabilitas|skor model|skor area|tingkat keyakinan)\s*[:=]?\s*\d+(?:[.,]\d+)?\s*(?:%|persen)?\)?', '', text)
    text = re.sub(r'(?i)\b\d+(?:[.,]\d+)?\s*(?:%|persen)\s*(?:yakin|keyakinan)\b', '', text)
    text = re.sub(r'(?im)^\s*(?:[-*#]+|\d+\))\s+', '', text).replace('**', '')
    sentences = re.split(r'(?<=[.!?])\s+', text)
    seen, output = set(), []
    for sentence in sentences:
        key = ' '.join(sentence.casefold().split())
        if key and key not in seen:
            seen.add(key)
            output.append(sentence.strip())
    return spoken_text(' '.join(output), max_words=max_words)
