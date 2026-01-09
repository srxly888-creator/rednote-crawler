import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlparse

import requests


@dataclass
class ImageDownloadOptions:
    root_dir: str = "images"
    max_side: Optional[int] = None
    format: str = "jpg"  # jpg|png|webp
    quality: int = 85
    timeout_sec: int = 30
    user_agent: str = "Mozilla/5.0"
    referer: str = "https://www.xiaohongshu.com/"


def _safe_filename_from_url(url: str, fallback: str) -> str:
    try:
        name = os.path.basename(urlparse(url).path)
        return name or fallback
    except Exception:
        return fallback


def _load_pillow():
    try:
        from PIL import Image  # type: ignore

        return Image
    except Exception:
        return None


def download_images(
    note_id: str,
    urls: Iterable[str],
    *,
    options: Optional[ImageDownloadOptions] = None,
) -> list[str]:
    """
    Download images to local disk.

    - If options.max_side is set and Pillow is installed, images will be resized to max_side (keep aspect ratio)
      and re-encoded to options.format/options.quality to save disk space.
    - If Pillow is not installed, it will save the original bytes as-is.

    Returns: list of saved file paths.
    """
    opts = options or ImageDownloadOptions()
    out_dir = Path(opts.root_dir) / str(note_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": opts.user_agent,
            "Referer": opts.referer,
        }
    )

    saved_paths: list[str] = []
    Image = _load_pillow()
    can_resize = bool(opts.max_side and Image)

    for i, url in enumerate(urls):
        if not url:
            continue
        fallback_name = f"{i:02d}.{opts.format}"
        name = _safe_filename_from_url(url, fallback_name)

        # When re-encoding, keep a predictable extension (avoid saving .webp as .jpg etc).
        if can_resize:
            filename = f"{i:02d}_{Path(name).stem}.{opts.format}"
        else:
            filename = f"{i:02d}_{name}"
        path = out_dir / filename

        resp = session.get(url, timeout=opts.timeout_sec)
        resp.raise_for_status()

        if can_resize:
            try:
                from io import BytesIO

                img = Image.open(BytesIO(resp.content))
                img.load()
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGB")
                if img.mode == "RGBA" and opts.format.lower() in ("jpg", "jpeg"):
                    img = img.convert("RGB")
                img.thumbnail((opts.max_side, opts.max_side))

                fmt = opts.format.upper()
                save_kwargs = {}
                if fmt in ("JPG", "JPEG", "WEBP"):
                    save_kwargs["quality"] = int(opts.quality)
                img.save(path, format=fmt, **save_kwargs)
            finally:
                try:
                    img.close()
                except Exception:
                    pass
        else:
            with open(path, "wb") as f:
                f.write(resp.content)

        saved_paths.append(str(path))

    return saved_paths
