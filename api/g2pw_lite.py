"""A model that reads a whole Chinese sentence and says how each character with more than
one reading is pronounced there (g2pW, Yi-Chang Chen et al., Apache-2.0:
https://github.com/GitYCC/g2pW).

This is the model's own arithmetic and nothing of its Python package, which needs PyTorch and
transformers (a gigabyte of libraries) to do what is, at reading time, one call of an ONNX
model: here it takes onnxruntime and numpy. The two small tables in tts_data/ come from that
package; the model itself (g2pw.onnx, 635 MB) and BERT's word list are fetched when a user
asks for them (tts.py).

The model was trained on Taiwan's readings. tts_text.decide() lets it decide a fixed list of
characters only and never a reading that differs across the strait.
"""
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
WINDOW = 32                       # characters of the sentence shown to the model around the one asked about


def wordpieces(word: str, vocab: dict) -> list:
    """BERT's way with a run of Latin letters or digits: the longest pieces its word list has."""
    out, i = [], 0
    while i < len(word):
        j = len(word)
        while j > i:
            piece = ("##" if i else "") + word[i:j]
            if piece in vocab:
                out.append((piece, j - i))
                break
            j -= 1
        else:
            return [("[UNK]", len(word))]
        i = j
    return out


def tokens_of(text: str, vocab: dict):
    """(tokens, for each character of text the number of its token or None). Latin letters and
    digits go by word, in pieces; everything else is one token a character; spaces are none."""
    tokens, at = [], [None] * len(text)
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == " ":
            i += 1
            continue
        m = re.match(r"[a-zA-Z0-9]+", text[i:])
        if m:
            w, pos = m.group(0), i
            for piece, n in wordpieces(w, vocab):
                for k in range(pos, min(pos + n, i + len(w))):
                    at[k] = len(tokens)
                tokens.append(piece)
                pos += n
            i += len(w)
        else:
            at[i] = len(tokens)
            tokens.append(ch if ch in vocab else "[UNK]")
            i += 1
    return tokens, at


class Reader:
    """reader(sentence, ask) -> {position: pinyin syllable} for the positions in `ask` the
    model has an opinion on."""

    def __init__(self, model_dir: str, vocab_path: str, threads: int = 2):
        import numpy as np
        import onnxruntime as ort
        self.np = np
        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        so.intra_op_num_threads = max(1, threads)
        self.session = ort.InferenceSession(os.path.join(model_dir, "g2pw.onnx"), sess_options=so, providers=["CPUExecutionProvider"])
        with open(vocab_path, encoding="utf-8") as f:
            self.vocab = {ln.rstrip("\n"): k for k, ln in enumerate(f)}
        with open(os.path.join(model_dir, "POLYPHONIC_CHARS.txt"), encoding="utf-8") as f:
            pairs = [ln.split("\t") for ln in f.read().strip().split("\n")]
        self.labels = sorted({p for _c, p in pairs})
        index = {p: k for k, p in enumerate(self.labels)}
        self.readings = {}
        for c, p in pairs:
            self.readings.setdefault(c, []).append(index[p])
        self.chars = sorted(self.readings)
        self.char_id = {c: k for k, c in enumerate(self.chars)}
        with open(os.path.join(HERE, "tts_data", "bopomofo_to_pinyin.json"), encoding="utf-8") as f:
            self.to_pinyin = json.load(f)
        self.traditional = {}
        with open(os.path.join(HERE, "tts_data", "simplified_to_traditional.txt"), encoding="utf-8") as f:
            for ln in f:
                p = ln.rstrip("\n").split("\t")
                if len(p) == 2:
                    self.traditional[p[0]] = p[1]

    def pinyin(self, bopomofo: str):
        base = self.to_pinyin.get(bopomofo[:-1])
        return (base + bopomofo[-1]).replace("u:", "v") if base and bopomofo[-1] in "12345" else None

    def __call__(self, sentence: str, ask=None) -> dict:
        np = self.np
        text = "".join(self.traditional.get(c, c) for c in sentence)        # the model was trained on traditional characters
        where = [i for i in (ask if ask is not None else range(len(text))) if 0 <= i < len(text) and text[i] in self.readings]
        if not where:
            return {}
        rows = []
        for i in where:
            start = max(0, i - WINDOW // 2)
            piece = text[start:min(len(text), i + WINDOW // 2)].lower()
            tokens, at = tokens_of(piece, self.vocab)
            pos = at[i - start]
            if pos is None:
                continue
            ids = [self.vocab["[CLS]"]] + [self.vocab.get(t, self.vocab["[UNK]"]) for t in tokens] + [self.vocab["[SEP]"]]
            mask = np.zeros(len(self.labels), dtype=np.float32)
            mask[self.readings[text[i]]] = 1.0
            rows.append((i, ids, mask, self.char_id[text[i]], pos + 1))
        if not rows:
            return {}
        out = {}
        for b in range(0, len(rows), 64):
            batch = rows[b:b + 64]
            width = max(len(r[1]) for r in batch)
            input_ids = np.zeros((len(batch), width), dtype=np.int64)
            attention = np.zeros((len(batch), width), dtype=np.int64)
            for k, r in enumerate(batch):
                input_ids[k, :len(r[1])] = r[1]
                attention[k, :len(r[1])] = 1
            probs = self.session.run([], {
                "input_ids": input_ids, "token_type_ids": np.zeros_like(input_ids), "attention_mask": attention,
                "phoneme_mask": np.stack([r[2] for r in batch]),
                "char_ids": np.array([r[3] for r in batch], dtype=np.int64),
                "position_ids": np.array([r[4] for r in batch], dtype=np.int64)})[0]
            for r, best in zip(batch, probs.argmax(axis=-1).tolist()):
                p = self.pinyin(self.labels[best])
                if p:
                    out[r[0]] = p
        return out
