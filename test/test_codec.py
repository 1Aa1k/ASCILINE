"""
Tests for codec.py — the adaptive raw/zlib/delta frame codec.

The shipped decoder lives in codec.js (browser/Node). These tests add a tiny
Python reference decoder so the encoder can be verified server-side too:

  * roundtrip: decoded frames match the source within the colour tolerance
    (character plane always exact),
  * size safety: a message is never larger than the raw frame + header,
  * output-preserving optimization: skipping the redundant full-frame zlib
    pass (the FPS fix) picks the exact same bytes a brute-force "always try
    both" encoder would.

    python -m unittest discover -s test
    pytest test/
"""
import os
import sys
import struct
import zlib
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import codec
from codec import TAG_RAW, TAG_ZLIB, TAG_DELTA


def decode(msg: bytes, prev: np.ndarray | None, shape: tuple[int, int, int]) -> np.ndarray:
    """Reference decoder mirroring codec.js: returns the frame the client shows."""
    frame_index, tag = struct.unpack(">IB", msg[:5])
    payload = msg[5:]
    rows, cols, C = shape
    if tag == TAG_RAW:
        return np.frombuffer(payload, np.uint8).reshape(shape).copy()
    if tag == TAG_ZLIB:
        return np.frombuffer(zlib.decompress(payload), np.uint8).reshape(shape).copy()
    if tag == TAG_DELTA:
        blob = zlib.decompress(payload)
        n = len(blob) // (4 + C)
        idx = np.frombuffer(blob[: n * 4], "<u4")
        vals = np.frombuffer(blob[n * 4 :], np.uint8).reshape(n, C)
        out = prev.copy()
        out.reshape(-1, C)[idx] = vals
        return out
    raise ValueError(f"unknown codec tag {tag}")


def _brute_encode(frame, prev, frame_index, level=codec.DEFAULT_LEVEL, tolerance=0):
    """Reference encoder that always tries both delta and full-frame, then picks
    the smallest. The optimized encoder must produce identical bytes."""
    raw = frame.tobytes()
    keyframe = prev is None or (frame_index % codec.KEYFRAME_INTERVAL == 0)
    if keyframe or prev.shape != frame.shape:
        return codec._full_frame(raw, frame_index, level), frame.copy()
    C = frame.shape[2]
    diff = np.abs(frame.astype(np.int16) - prev.astype(np.int16))
    if C == 4:
        char_changed = frame[:, :, 0] != prev[:, :, 0]
        color_changed = (np.any(diff[:, :, 1:] != 0, axis=2) if tolerance <= 0
                         else np.any(diff[:, :, 1:] > tolerance, axis=2))
        changed = char_changed | color_changed
    else:
        changed = (np.any(diff != 0, axis=2) if tolerance <= 0
                   else np.any(diff > tolerance, axis=2))
    frac = float(changed.mean())
    ci = np.nonzero(changed.reshape(-1))[0].astype("<u4")
    delta_shown = prev.copy()
    delta_shown.reshape(-1, C)[ci] = frame.reshape(-1, C)[ci]
    candidates = []
    if frac < codec._DELTA_MAX_FRAC:
        vals = frame.reshape(-1, C)[ci]
        candidates.append((TAG_DELTA,
                           zlib.compress(ci.tobytes() + vals.tobytes(), level),
                           delta_shown))
    candidates.append((TAG_ZLIB, zlib.compress(raw, level), frame))
    tag, payload, shown = min(candidates, key=lambda c: len(c[1]))
    if len(raw) < len(payload):
        tag, payload, shown = TAG_RAW, raw, frame
    return struct.pack(">IB", frame_index, tag) + payload, (
        shown.copy() if shown is frame else shown)


def _make_stream(n, motion, rng, rows=80, cols=140, C=4, drift_hi=10):
    base = rng.integers(0, 256, (rows, cols, C), dtype=np.uint8)
    if C == 4:
        base[:, :, 0] = rng.integers(32, 127, (rows, cols))  # char plane
    frames = [base.copy()]
    for _ in range(n - 1):
        f = frames[-1].copy()
        mask = rng.random((rows, cols)) < motion
        m = int(mask.sum())
        drift = rng.integers(-drift_hi, drift_hi + 1, (m, C if C == 3 else 3)).astype(np.int16)
        sl = f if C == 3 else f[:, :, 1:]
        sl[mask] = np.clip(sl[mask].astype(np.int16) + drift, 0, 255).astype(np.uint8)
        frames.append(f)
    return frames


class CodecTests(unittest.TestCase):
    def test_roundtrip_within_tolerance(self):
        rng = np.random.default_rng(1)
        for C in (4, 3):
            for tol in (0, 4, 8, 16):
                frames = _make_stream(60, 0.4, rng, C=C)
                shape = frames[0].shape
                enc_prev = None
                dec_prev = None
                for i, fr in enumerate(frames):
                    fr = np.ascontiguousarray(fr)
                    msg, enc_prev = codec.encode_frame(fr, enc_prev, i, tolerance=tol)
                    shown = decode(msg, dec_prev, shape)
                    dec_prev = shown
                    # encoder's notion of what the client shows must match the decode
                    self.assertTrue(np.array_equal(shown, enc_prev),
                                    f"shown mismatch C={C} tol={tol} frame={i}")
                    if C == 4:
                        # character plane is always exact
                        self.assertTrue(np.array_equal(shown[:, :, 0], fr[:, :, 0]))

    def test_message_never_exceeds_raw(self):
        rng = np.random.default_rng(2)
        frames = _make_stream(40, 0.5, rng)
        raw_len = frames[0].size
        prev = None
        for i, fr in enumerate(frames):
            fr = np.ascontiguousarray(fr)
            msg, prev = codec.encode_frame(fr, prev, i, tolerance=8)
            self.assertLessEqual(len(msg), raw_len + 5)  # +5 byte header

    def test_optimization_is_output_preserving(self):
        """The CPU optimization must pick byte-identical messages to a
        brute-force encoder that always evaluates both candidates."""
        rng = np.random.default_rng(3)
        for motion in (0.15, 0.35, 0.55, 0.8):
            for tol in (0, 8, 16):
                frames = _make_stream(50, motion, rng)
                opt_prev = None
                ref_prev = None
                for i, fr in enumerate(frames):
                    fr = np.ascontiguousarray(fr)
                    opt_msg, opt_prev = codec.encode_frame(fr, opt_prev, i, tolerance=tol)
                    ref_msg, ref_prev = _brute_encode(fr, ref_prev, i, tolerance=tol)
                    self.assertEqual(opt_msg, ref_msg,
                                     f"diverged motion={motion} tol={tol} frame={i}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
