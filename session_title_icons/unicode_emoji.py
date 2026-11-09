"""Unicode Emoji 16.0 conformance data, not a semantic selection catalog.

Recognizes whole standardized sequences (including flags, keycaps and ZWJ),
never generic symbol/codepoint ranges. Components alone are not icons.
"""
import hashlib
from pathlib import Path

SHA256 = '24f0c534e86cf142e2496953e8f0e46a3e702392911eddcd29c6cced85139697'


class Emoji:
    def __init__(self):
        data = (Path(__file__).parent / 'data' / 'emoji-test.txt').read_bytes()
        if hashlib.sha256(data).hexdigest() != SHA256:
            raise ValueError('Unicode data integrity failure')
        self.sequences = set()
        for line in data.decode('utf-8').splitlines():
            spec = line.split('#', 1)[0].strip()
            if not spec:
                continue
            points, status = (part.strip() for part in spec.split(';'))
            if status in {'fully-qualified', 'minimally-qualified', 'unqualified'}:
                self.sequences.add(''.join(chr(int(point, 16)) for point in points.split()))
        self.max_length = max(map(len, self.sequences))

    def is_emoji(self, value):
        return isinstance(value, str) and value in self.sequences

    def first(self, text):
        # Longest standardized emoji prefix; a non-emoji prefix stays non-emoji.
        for length in range(min(len(text), self.max_length), 0, -1):
            if text[:length] in self.sequences:
                return text[:length]
        return text[:1]
