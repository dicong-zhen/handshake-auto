"""AI vision client supporting multiple providers.

Most providers expose an OpenAI-compatible chat-completions endpoint, so they
all go through the ``openai`` SDK with the right ``base_url``. Anthropic (Claude)
uses its native SDK.

Add a provider by extending :data:`PROVIDERS`.
"""

from __future__ import annotations

import base64
import io
import json
import re
from typing import Optional

from PIL import Image


class AIError(Exception):
    """Raised when the AI request cannot be completed."""


# Provider registry. ``native`` marks providers handled outside the OpenAI SDK.
PROVIDERS: dict[str, dict] = {
    "openai": {
        "label": "OpenAI",
        "base_url": None,
        "default_model": "gpt-4o-mini",
        "key_env": "OPENAI_API_KEY",
    },
    "anthropic": {
        "label": "Anthropic (Claude)",
        "base_url": None,
        "default_model": "claude-3-5-sonnet-latest",
        "key_env": "ANTHROPIC_API_KEY",
        "native": "anthropic",
    },
    "gemini": {
        "label": "Google Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "default_model": "gemini-1.5-flash",
        "key_env": "GEMINI_API_KEY",
    },
    "openrouter": {
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "default_model": "openai/gpt-4o-mini",
        "key_env": "OPENROUTER_API_KEY",
    },
    "custom": {
        "label": "Custom (OpenAI-compatible)",
        "base_url": None,
        "default_model": "gpt-4o-mini",
        "key_env": "OPENAI_API_KEY",
    },
}

DEFAULT_PROVIDER = "openai"


def provider_labels() -> list[str]:
    return [info["label"] for info in PROVIDERS.values()]


def provider_from_label(label: str) -> str:
    for key, info in PROVIDERS.items():
        if info["label"] == label:
            return key
    return DEFAULT_PROVIDER


def label_for(provider: str) -> str:
    return PROVIDERS.get(provider, PROVIDERS[DEFAULT_PROVIDER])["label"]


def default_model(provider: str) -> str:
    return PROVIDERS.get(provider, {}).get("default_model", "gpt-4o-mini")


def _image_to_data_url(image: Image.Image) -> str:
    return f"data:image/png;base64,{_image_b64(image)}"


def _image_b64(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _resolve_base_url(provider: str, base_url: Optional[str]) -> Optional[str]:
    if base_url:
        return base_url
    return PROVIDERS.get(provider, {}).get("base_url")


# ----------------------------------------------------------------------
# Unified chat call
# ----------------------------------------------------------------------
def _chat(
    image: Image.Image,
    prompt: str,
    *,
    provider: str,
    api_key: str,
    model: str,
    base_url: Optional[str],
    max_tokens: int,
) -> str:
    if not api_key:
        raise AIError(f"No API key set for {label_for(provider)}. Add it on the Settings tab.")

    info = PROVIDERS.get(provider, PROVIDERS[DEFAULT_PROVIDER])
    if info.get("native") == "anthropic":
        return _chat_anthropic(image, prompt, api_key=api_key, model=model,
                               base_url=base_url, max_tokens=max_tokens)
    return _chat_openai(image, prompt, provider=provider, api_key=api_key,
                        model=model, base_url=base_url, max_tokens=max_tokens)


def _model_hint(msg: str) -> str:
    low = msg.lower()
    if "model" in low and ("not_found" in low or "404" in low or "does not exist" in low):
        return msg + "  → Click 'Fetch' next to Model in Settings to list valid model names."
    return msg


def list_models(provider: str, *, api_key: str, base_url: Optional[str] = None) -> list[str]:
    """Return the model IDs available for a provider/key (sorted)."""
    if not api_key:
        raise AIError(f"Enter an API key for {label_for(provider)} first.")
    info = PROVIDERS.get(provider, PROVIDERS[DEFAULT_PROVIDER])
    try:
        if info.get("native") == "anthropic":
            import anthropic
            kwargs = {"api_key": api_key}
            if base_url:
                kwargs["base_url"] = base_url
            result = anthropic.Anthropic(**kwargs).models.list()
        else:
            from openai import OpenAI
            kwargs = {"api_key": api_key}
            resolved = _resolve_base_url(provider, base_url)
            if resolved:
                kwargs["base_url"] = resolved
            result = OpenAI(**kwargs).models.list()
    except Exception as exc:  # noqa: BLE001
        raise AIError(str(exc)) from exc

    ids = [getattr(m, "id", None) or (m.get("id") if isinstance(m, dict) else None)
           for m in getattr(result, "data", [])]
    return sorted([i for i in ids if i])


def _chat_openai(image, prompt, *, provider, api_key, model, base_url, max_tokens) -> str:
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover
        raise AIError("The 'openai' package is not installed.") from exc

    kwargs = {"api_key": api_key}
    resolved = _resolve_base_url(provider, base_url)
    if resolved:
        kwargs["base_url"] = resolved
    client = OpenAI(**kwargs)

    try:
        response = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url",
                         "image_url": {"url": _image_to_data_url(image)}},
                    ],
                }
            ],
        )
    except Exception as exc:  # noqa: BLE001
        raise AIError(_model_hint(str(exc))) from exc

    if not response.choices:
        raise AIError("The model returned no choices.")
    return (response.choices[0].message.content or "").strip()


def _chat_anthropic(image, prompt, *, api_key, model, base_url, max_tokens) -> str:
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover
        raise AIError("The 'anthropic' package is not installed.") from exc

    kwargs = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    client = anthropic.Anthropic(**kwargs)

    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {
                            "type": "base64", "media_type": "image/png",
                            "data": _image_b64(image)}},
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        )
    except Exception as exc:  # noqa: BLE001
        raise AIError(_model_hint(str(exc))) from exc

    parts = [block.text for block in response.content if getattr(block, "type", None) == "text"]
    return "".join(parts).strip()


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------
def ask(
    image: Image.Image,
    prompt: str,
    *,
    api_key: str,
    model: str = "gpt-4o-mini",
    base_url: Optional[str] = None,
    provider: str = DEFAULT_PROVIDER,
    max_tokens: int = 500,
) -> str:
    """Send an image + prompt to a vision model and return the text answer."""
    return _chat(image, prompt, provider=provider, api_key=api_key,
                 model=model, base_url=base_url, max_tokens=max_tokens)


def _chat_text(prompt: str, *, provider: str, api_key: str, model: str,
               base_url: Optional[str], max_tokens: int) -> str:
    """Text-only chat completion (no image), routed like :func:`_chat`."""
    if not api_key:
        raise AIError(f"No API key set for {label_for(provider)}. Add it on the Settings tab.")
    info = PROVIDERS.get(provider, PROVIDERS[DEFAULT_PROVIDER])
    if info.get("native") == "anthropic":
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover
            raise AIError("The 'anthropic' package is not installed.") from exc
        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        client = anthropic.Anthropic(**kwargs)
        try:
            response = client.messages.create(
                model=model, max_tokens=max_tokens,
                messages=[{"role": "user", "content": [{"type": "text", "text": prompt}]}],
            )
        except Exception as exc:  # noqa: BLE001
            raise AIError(_model_hint(str(exc))) from exc
        parts = [b.text for b in response.content if getattr(b, "type", None) == "text"]
        return "".join(parts).strip()

    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover
        raise AIError("The 'openai' package is not installed.") from exc
    kwargs = {"api_key": api_key}
    resolved = _resolve_base_url(provider, base_url)
    if resolved:
        kwargs["base_url"] = resolved
    client = OpenAI(**kwargs)
    try:
        response = client.chat.completions.create(
            model=model, max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:  # noqa: BLE001
        raise AIError(_model_hint(str(exc))) from exc
    if not response.choices:
        raise AIError("The model returned no choices.")
    return (response.choices[0].message.content or "").strip()


def ask_text(
    prompt: str,
    *,
    api_key: str,
    model: str = "gpt-4o-mini",
    base_url: Optional[str] = None,
    provider: str = DEFAULT_PROVIDER,
    max_tokens: int = 200,
) -> str:
    """Send a text-only prompt to the model and return its reply."""
    return _chat_text(prompt, provider=provider, api_key=api_key, model=model,
                      base_url=base_url, max_tokens=max_tokens)


def classify(
    text: str,
    options: list[str],
    *,
    instruction: str = "",
    api_key: str,
    model: str = "gpt-4o-mini",
    base_url: Optional[str] = None,
    provider: str = DEFAULT_PROVIDER,
    log=None,
) -> Optional[str]:
    """Ask the model which ONE of ``options`` best applies to ``text``.

    Returns the matching option string (exactly as supplied) or ``None`` if the
    model decides none applies.
    """
    opts = [o.strip() for o in options if o and o.strip()]
    if not opts:
        return None
    numbered = "\n".join(f"{i + 1}. {o}" for i, o in enumerate(opts))
    prompt = (
        (instruction.strip() + "\n\n" if instruction.strip() else "")
        + "Read the TEXT below and decide which ONE of these options best "
        "applies. Reply with ONLY the option's number (a single integer), "
        "or 0 if none applies.\n\n"
        f"Options:\n{numbered}\n\n"
        "----- TEXT -----\n"
        f"{text}\n"
        "----- END TEXT -----\n"
        "Answer with just the number:"
    )
    raw = ask_text(prompt, api_key=api_key, model=model, base_url=base_url,
                   provider=provider, max_tokens=10)
    if log is not None:
        log(f"  AI decision reply: {raw.strip()[:80]}")
    m = re.search(r"-?\d+", raw)
    if m:
        idx = int(m.group(0))
        if 1 <= idx <= len(opts):
            return opts[idx - 1]
        return None
    # Fallback: the model echoed an option label instead of a number.
    low = raw.strip().lower()
    for o in opts:
        if o.lower() == low:
            return o
    for o in opts:
        if o.lower() in low:
            return o
    return None


def check_condition(
    image: Image.Image,
    condition: str,
    *,
    api_key: str,
    model: str = "gpt-4o-mini",
    base_url: Optional[str] = None,
    provider: str = DEFAULT_PROVIDER,
    log=None,
) -> tuple[Optional[bool], str]:
    """Ask the model whether ``condition`` is true of the screenshot.

    Returns ``(met, reason)`` where ``met`` is ``True``/``False``, or ``None`` if
    the reply could not be parsed.
    """
    prompt = (
        "You are a strict QA assistant verifying the state of a screen. "
        f"Condition to verify: \"{condition}\".\n"
        "Look carefully at the screenshot and decide whether the condition is "
        "clearly TRUE. Respond with ONLY compact JSON and nothing else:\n"
        '{"met": true, "reason": "<short explanation>"}\n'
        "Set \"met\" to false if the condition is not clearly satisfied."
    )
    raw = _chat(image, prompt, provider=provider, api_key=api_key,
                model=model, base_url=base_url, max_tokens=160)
    if log is not None:
        log(f"  AI check reply: {raw.strip()[:200]}")

    data = _extract_json(raw)
    if data is not None and "met" in data:
        val = data["met"]
        if isinstance(val, str):
            val = val.strip().lower() in ("true", "yes", "1", "met", "pass")
        return bool(val), str(data.get("reason", "")).strip()

    low = raw.lower()
    if re.search(r"\b(not met|false|^no\b|fail)\b", low):
        return False, raw.strip()
    if re.search(r"\b(met|true|yes|pass)\b", low):
        return True, raw.strip()
    return None, raw.strip()


def _extract_json(text: str) -> Optional[dict]:
    """Pull the first JSON object out of a model response (handles code fences)."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return None


_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _extract_four_numbers(text: str) -> Optional[tuple[float, float, float, float]]:
    """Pull four numbers out of a non-JSON grounding reply.

    Handles formats some models use instead of our JSON, e.g.
    ``<bbox>119, 249, 1005, 299</bbox>`` or ``[119, 249, 1005, 299]``.
    Prefers numbers inside a ``<bbox>`` tag or the first bracketed list, then
    falls back to the first four numbers anywhere in the reply.
    """
    region: Optional[str] = None
    m = re.search(r"<bbox>(.*?)</bbox>", text, flags=re.IGNORECASE | re.DOTALL)
    if m:
        region = m.group(1)
    else:
        m = re.search(r"\[([^\[\]]*?)\]", text, flags=re.DOTALL)
        if m:
            region = m.group(1)
    nums = _NUM_RE.findall(region if region is not None else text)
    if len(nums) < 4:
        return None
    try:
        a, b, c, d = (float(n) for n in nums[:4])
    except ValueError:
        return None
    return (a, b, c, d)


def _locate_once(
    image: Image.Image, description: str, *, provider, api_key, model, base_url, log,
) -> Optional[tuple[float, float]]:
    """One localisation pass; returns ``(fx, fy)`` fractions or ``None``."""
    w, h = image.width, image.height
    prompt = (
        "You are a precise UI-grounding assistant. "
        f"This screenshot is exactly {w} pixels wide and {h} pixels tall. "
        f"The top-left corner is (0, 0) and the bottom-right corner is ({w}, {h}).\n"
        f"Find this element: \"{description}\".\n"
        "Respond with ONLY compact JSON and nothing else, in the form:\n"
        '{"found": true, "x": <integer>, "y": <integer>}\n'
        f"where x and y are the CENTRE of the element in PIXEL coordinates within "
        f"this image (x between 0 and {w}, y between 0 and {h}). "
        'If the element is not visible, respond {"found": false}.'
    )
    raw = _chat(image, prompt, provider=provider, api_key=api_key,
                model=model, base_url=base_url, max_tokens=100)
    if log is not None:
        snippet = raw.strip().replace("\n", " ")
        log(f"  AI raw reply: {snippet[:200]}")

    data = _extract_json(raw)
    if not data or not data.get("found", False):
        return None
    try:
        x = float(data["x"])
        y = float(data["y"])
    except (KeyError, TypeError, ValueError):
        return None
    # We ask for pixels, but tolerate fraction replies (both <= 1.0).
    if 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0:
        fx, fy = x, y
    else:
        fx, fy = x / w, y / h
    return (min(1.0, max(0.0, fx)), min(1.0, max(0.0, fy)))


def _locate_region_once(
    image: Image.Image, description: str, *, provider, api_key, model, base_url, log,
) -> Optional[tuple[int, int, int, int]]:
    """One bounding-box pass; returns ``(left, top, right, bottom)`` in PIXELS."""
    w, h = image.width, image.height
    prompt = (
        "You are a precise UI-grounding assistant that returns bounding boxes. "
        f"This screenshot is exactly {w} pixels wide and {h} pixels tall. "
        f"The top-left corner is (0, 0) and the bottom-right corner is ({w}, {h}).\n"
        f"Find this region: \"{description}\".\n"
        "Return the SMALLEST rectangle that tightly encloses ONLY that region — "
        "do not include neighbouring cards, headers, banners, or whitespace. "
        "If several things could match, choose the one that best fits the "
        "description and ignore the others.\n"
        "Respond with ONLY compact JSON and nothing else, in the form:\n"
        '{"found": true, "label": "<few words naming what you boxed>", '
        '"x1": <int>, "y1": <int>, "x2": <int>, "y2": <int>}\n'
        f"where (x1, y1) is the TOP-LEFT and (x2, y2) the BOTTOM-RIGHT corner in "
        f"PIXEL coordinates within this image (0..{w} horizontally, 0..{h} "
        "vertically), with x2 > x1 and y2 > y1. "
        'If the region is not visible, respond {"found": false}.'
    )
    raw = _chat(image, prompt, provider=provider, api_key=api_key,
                model=model, base_url=base_url, max_tokens=120)
    if log is not None:
        snippet = raw.strip().replace("\n", " ")
        log(f"  AI raw reply: {snippet[:200]}")

    data = _extract_json(raw)
    coords: Optional[tuple[float, float, float, float]] = None
    if data is not None:
        if data.get("found") is False:
            return None  # model explicitly says it's not visible
        if data.get("label") and log is not None:
            log(f"  AI boxed: \"{data['label']}\"")
        try:
            coords = (float(data["x1"]), float(data["y1"]),
                      float(data["x2"]), float(data["y2"]))
        except (KeyError, TypeError, ValueError):
            coords = None
    if coords is None:
        # Model replied in a non-JSON grounding format (e.g. <bbox>…</bbox>).
        coords = _extract_four_numbers(raw)
    if coords is None:
        return None
    x1, y1, x2, y2 = coords
    # We ask for pixels, but tolerate fraction replies (all between 0 and 1).
    if all(0.0 <= v <= 1.0 for v in (x1, y1, x2, y2)):
        x1, x2, y1, y2 = x1 * w, x2 * w, y1 * h, y2 * h
    left = int(max(0, min(x1, x2)))
    right = int(min(w, max(x1, x2)))
    top = int(max(0, min(y1, y2)))
    bottom = int(min(h, max(y1, y2)))
    if right - left < 2 or bottom - top < 2:
        return None
    return (left, top, right, bottom)


def locate_region(
    image: Image.Image,
    description: str,
    *,
    api_key: str,
    model: str = "gpt-4o-mini",
    base_url: Optional[str] = None,
    provider: str = DEFAULT_PROVIDER,
    pad: int = 0,
    refine: bool = True,
    log=None,
) -> Optional[tuple[int, int, int, int]]:
    """Ask the model for the bounding box of a described region.

    Returns ``(left, top, right, bottom)`` in image PIXEL coordinates (suitable
    for ``PIL.Image.crop``), optionally expanded by ``pad`` pixels on every
    side, or ``None`` if the model can't find it.

    When ``refine`` is set, a second "zoom-in" pass runs: the image is cropped
    generously around the first guess (and upscaled if small) and queried again.
    Coordinates within a smaller image are much more accurate, which greatly
    improves precision for cards in large, downscaled screenshots.
    """
    box = _locate_region_once(image, description, provider=provider, api_key=api_key,
                              model=model, base_url=base_url, log=log)
    if box is None:
        return None

    if refine:
        bw, bh = box[2] - box[0], box[3] - box[1]
        mx, my = int(bw * 0.6) + 24, int(bh * 0.6) + 24
        cl, ct = max(0, box[0] - mx), max(0, box[1] - my)
        cr, cb = min(image.width, box[2] + mx), min(image.height, box[3] + my)
        # Only worth refining if the crop is meaningfully smaller than the whole.
        if (cr - cl) < image.width * 0.92 or (cb - ct) < image.height * 0.92:
            crop = image.crop((cl, ct, cr, cb))
            scale = 1.0
            longest = max(crop.width, crop.height)
            if longest < 900:  # upscale small crops so the model sees detail
                scale = 900 / longest
                crop = crop.resize((max(1, int(crop.width * scale)),
                                    max(1, int(crop.height * scale))))
            if log is not None:
                log("  Refining crop box (zoom-in pass)…")
            try:
                box2 = _locate_region_once(crop, description, provider=provider,
                                           api_key=api_key, model=model,
                                           base_url=base_url, log=log)
            except AIError:
                box2 = None
            if box2 is not None:
                box = (
                    cl + int(box2[0] / scale), ct + int(box2[1] / scale),
                    cl + int(box2[2] / scale), ct + int(box2[3] / scale),
                )

    left = max(0, box[0] - pad)
    top = max(0, box[1] - pad)
    right = min(image.width, box[2] + pad)
    bottom = min(image.height, box[3] + pad)
    return (left, top, right, bottom)


def locate(
    image: Image.Image,
    description: str,
    *,
    api_key: str,
    model: str = "gpt-4o-mini",
    base_url: Optional[str] = None,
    provider: str = DEFAULT_PROVIDER,
    refine: bool = True,
    log=None,
) -> Optional[tuple[float, float]]:
    """Ask the model where a described UI element is in the image.

    Returns the element centre as ``(fx, fy)`` fractions of the whole image
    (both in ``0..1``), or ``None`` if the model can't find it.

    When ``refine`` is set, a second "zoom-in" pass is performed: the image is
    cropped around the first guess and queried again, which greatly improves
    precision for small targets in large screenshots (costs one extra call).
    """
    loc = _locate_once(image, description, provider=provider, api_key=api_key,
                       model=model, base_url=base_url, log=log)
    if loc is None or not refine:
        return loc

    # Crop a window around the first guess and ask again for finer precision.
    frac = 0.35
    cw, ch = image.width * frac, image.height * frac
    if cw < 40 or ch < 40:  # image already tiny; refining won't help
        return loc
    cx, cy = loc[0] * image.width, loc[1] * image.height
    left = int(min(max(0, cx - cw / 2), image.width - cw))
    top = int(min(max(0, cy - ch / 2), image.height - ch))
    crop = image.crop((left, top, left + int(cw), top + int(ch)))
    if log is not None:
        log("  Refining (zoom-in pass)…")
    try:
        loc2 = _locate_once(crop, description, provider=provider, api_key=api_key,
                            model=model, base_url=base_url, log=log)
    except AIError:
        return loc
    if loc2 is None:
        return loc
    abs_x = (left + loc2[0] * crop.width) / image.width
    abs_y = (top + loc2[1] * crop.height) / image.height
    return (min(1.0, max(0.0, abs_x)), min(1.0, max(0.0, abs_y)))
