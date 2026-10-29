"""Screenshot storage and capture (CDP for HD, ImageMagick for a native X11 grab)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from dda import cdp

NAME_RE = re.compile(r"^discord-[0-9A-Za-z_.-]+\.png$")


class ShotStore:
    def __init__(self, directory: Path, keep: int, cdp_port: int, display: str, max_width: int) -> None:
        self.dir = directory
        self.keep = keep
        self.cdp_port = cdp_port
        self.display = display
        self.max_width = max_width
        self._lock = threading.Lock()  # one capture at a time: 8K renders are memory hungry
        self.dir.mkdir(parents=True, exist_ok=True)

    def _name(self, tag: str, px: int) -> str:
        base = time.strftime("discord-%Y%m%d-%H%M%S") + f"-{tag}-{px}px"
        name, n = base + ".png", 1
        while (self.dir / name).exists():
            n += 1
            name = f"{base}-{n}.png"
        return name

    def list(self) -> List[Dict[str, object]]:
        rows = []
        for entry in os.scandir(self.dir):
            if NAME_RE.match(entry.name):
                st = entry.stat()
                rows.append({"name": entry.name, "bytes": st.st_size, "mtime": st.st_mtime})
        rows.sort(key=lambda r: r["mtime"], reverse=True)  # type: ignore[arg-type,return-value]
        return rows

    def path(self, name: str) -> Optional[Path]:
        if not NAME_RE.match(name):
            return None
        p = self.dir / name
        return p if p.is_file() else None

    def delete(self, name: str) -> bool:
        p = self.path(name)
        if not p:
            return False
        p.unlink()
        return True

    def _prune(self) -> None:
        for row in self.list()[self.keep :]:
            try:
                (self.dir / str(row["name"])).unlink()
            except OSError:
                pass

    def capture_hd(self, width: int) -> Dict[str, object]:
        width = max(640, min(width, self.max_width))
        with self._lock:
            data, w, h = cdp.capture_png(self.cdp_port, width)
            name = self._name("hd", w)
            (self.dir / name).write_bytes(data)
            self._prune()
        return {"name": name, "dims": f"{w}x{h}", "bytes": len(data)}

    def capture_x11(self, width: int = 0) -> Dict[str, object]:
        grab = shutil.which("import")
        if not grab:
            raise RuntimeError("ImageMagick 'import' is not installed")
        width = max(0, min(width, self.max_width))
        with self._lock:
            name = self._name("x11", width)
            path = self.dir / name
            env = dict(os.environ, DISPLAY=self.display)
            subprocess.run(
                [grab, "-display", self.display, "-window", "root", str(path)], check=True, timeout=30, env=env
            )
            dims = "native"
            resizer = shutil.which("magick") or shutil.which("convert")
            if width and resizer:
                cmd = (
                    [resizer]
                    + (["convert"] if resizer.endswith("magick") else [])
                    + [str(path), "-filter", "Lanczos", "-resize", f"{width}x", str(path)]
                )
                subprocess.run(cmd, check=True, timeout=60)
                dims = f"{width}px (upscaled)"
            self._prune()
        return {"name": name, "dims": dims, "bytes": path.stat().st_size}
