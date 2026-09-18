import io
import json
from pathlib import Path
from typing import Optional

import numpy as np
import onnxruntime as ort
from PIL import Image

import config

_CRNN_JSON_PATH = config.MODEL_DIR / "crnn.json"
_CRNN_ONNX_PATH = config.MODEL_DIR / "crnn.onnx"

_captcha_meta: Optional[dict] = None
_captcha_session: Optional[ort.InferenceSession] = None


def _load_model() -> None:
    global _captcha_meta, _captcha_session
    if _captcha_meta is not None and _captcha_session is not None:
        return

    with open(_CRNN_JSON_PATH, "r", encoding="utf-8") as f:
        _captcha_meta = json.load(f)

    opts = ort.SessionOptions()
    opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    opts.intra_op_num_threads = 2
    _captcha_session = ort.InferenceSession(str(_CRNN_ONNX_PATH), sess_options=opts)


def warmup_captcha_solver() -> None:
    """Preload and warm up the ONNX inference session at startup to eliminate cold-start latency."""
    _load_model()


def solve_captcha(image_bytes: bytes) -> str:
    """
    Solve a KL ERP image CAPTCHA using the local ONNX CRNN model.
    """
    _load_model()

    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    img = img.resize((_captcha_meta["img_w"], _captcha_meta["img_h"]))

    # The model was trained on the alpha channel (transparent text on background).
    alpha = np.array(img)[:, :, 3].astype(np.float32) / 255.0
    tensor = alpha[np.newaxis, np.newaxis, :, :]

    ort_inputs = {_captcha_session.get_inputs()[0].name: tensor}
    logits = _captcha_session.run(None, ort_inputs)[0][0]

    T, C = logits.shape
    alphabet = _captcha_meta["alphabet"]
    blank_idx = _captcha_meta.get("blank_idx", 0)

    out = []
    last = -1
    for t in range(T):
        best = int(np.argmax(logits[t]))
        if best != last and best != blank_idx:
            out.append(alphabet[best - 1])
        last = best

    return "".join(out)


async def solve_captcha_async(image_bytes: bytes) -> str:
    """
    Asynchronously solve a KL ERP image CAPTCHA offloaded to a worker thread.
    Keeps the asyncio event loop 100% responsive.
    """
    import asyncio
    return await asyncio.to_thread(solve_captcha, image_bytes)
