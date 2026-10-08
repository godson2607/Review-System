"""Turns an uploaded file (single file or .zip project) into Claude content blocks."""
import base64
import zipfile
from pathlib import Path

TEXT_EXTENSIONS = {
    ".py", ".java", ".js", ".jsx", ".ts", ".tsx", ".html", ".htm", ".css", ".scss", ".c", ".cpp",
    ".h", ".hpp", ".cs", ".go", ".rb", ".php", ".kt", ".swift", ".rs", ".sql", ".sh", ".bat", ".ps1",
    ".json", ".xml", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".env.example", ".md", ".txt", ".csv",
    ".ipynb", ".r", ".dart", ".vue", ".svelte", ".gradle", ".properties",
}
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
SKIP_DIRS = {"node_modules", ".git", "__pycache__", "venv", ".venv", "dist", "build", ".idea", ".vscode", "target"}

ALLOWED_UPLOADS = TEXT_EXTENSIONS | set(IMAGE_TYPES) | {".pdf", ".zip"}

MAX_TEXT_CHARS = 600_000   # total code/text sent to the reviewer
MAX_FILE_CHARS = 150_000   # a single text file
MAX_BINARY_BYTES = 8 * 1024 * 1024
MAX_IMAGES = 10


class UnreadableSubmission(Exception):
    pass


def _ext(name):
    name = name.lower()
    if name.endswith(".env.example"):
        return ".env.example"
    return Path(name).suffix


def _decode(data):
    for enc in ("utf-8", "utf-16", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


class _Collector:
    def __init__(self):
        self.blocks = []
        self.included = []
        self.skipped = []
        self.text_chars = 0
        self.images = 0

    def add(self, name, data):
        ext = _ext(name)
        if ext in TEXT_EXTENSIONS:
            text = _decode(data)
            if text is None:
                self.skipped.append(f"{name} (could not read text)")
            elif len(text) > MAX_FILE_CHARS:
                self.skipped.append(f"{name} (file too large: {len(text):,} characters)")
            elif self.text_chars + len(text) > MAX_TEXT_CHARS:
                self.skipped.append(f"{name} (submission size limit reached)")
            else:
                self.text_chars += len(text)
                self.blocks.append({"type": "text", "text": f"===== FILE: {name} =====\n{text}"})
                self.included.append(name)
        elif ext == ".pdf":
            if len(data) > MAX_BINARY_BYTES:
                self.skipped.append(f"{name} (PDF too large)")
                return
            self.blocks.append({"type": "text", "text": f"===== FILE: {name} (PDF below) ====="})
            self.blocks.append({"type": "document", "source": {
                "type": "base64", "media_type": "application/pdf",
                "data": base64.standard_b64encode(data).decode()}})
            self.included.append(name)
        elif ext in IMAGE_TYPES:
            if len(data) > 5 * 1024 * 1024 or self.images >= MAX_IMAGES:
                self.skipped.append(f"{name} (image too large or too many images)")
                return
            self.images += 1
            self.blocks.append({"type": "text", "text": f"===== FILE: {name} (image below) ====="})
            self.blocks.append({"type": "image", "source": {
                "type": "base64", "media_type": IMAGE_TYPES[ext],
                "data": base64.standard_b64encode(data).decode()}})
            self.included.append(name)
        else:
            self.skipped.append(f"{name} (unsupported file type)")


def extract(path, original_name):
    """Returns (content_blocks, included_files, skipped_files)."""
    path = Path(path)
    c = _Collector()
    if _ext(original_name) == ".zip":
        with zipfile.ZipFile(path) as zf:
            for info in sorted(zf.infolist(), key=lambda i: i.filename):
                if info.is_dir():
                    continue
                parts = Path(info.filename).parts
                if any(p in SKIP_DIRS or p.startswith("__MACOSX") for p in parts):
                    continue
                if info.file_size > MAX_BINARY_BYTES:
                    c.skipped.append(f"{info.filename} (file too large)")
                    continue
                c.add(info.filename, zf.read(info))
    else:
        c.add(original_name, path.read_bytes())

    if not c.blocks:
        raise UnreadableSubmission(
            "No readable files were found in the submission. Skipped: " + (", ".join(c.skipped) or "none"))
    return c.blocks, c.included, c.skipped
