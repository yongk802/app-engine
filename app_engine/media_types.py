"""Web asset MIME types must not inherit desktop file-association overrides."""
import mimetypes
from pathlib import Path

_WEB_TYPES = {
    '.html': 'text/html', '.css': 'text/css',
    '.js': 'text/javascript', '.mjs': 'text/javascript',
    '.json': 'application/json', '.wasm': 'application/wasm',
    '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg', '.webp': 'image/webp',
    '.woff': 'font/woff', '.woff2': 'font/woff2',
    '.mp3': 'audio/mpeg', '.wav': 'audio/wav', '.ogg': 'audio/ogg',
    '.mp4': 'video/mp4', '.webm': 'video/webm',
}


def asset_media_type(path: str | Path) -> str:
    return _WEB_TYPES.get(Path(path).suffix.lower()) or mimetypes.guess_type(str(path))[0] or 'application/octet-stream'
