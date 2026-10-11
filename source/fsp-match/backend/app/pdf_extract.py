"""Isolated, bounded PDF text extractor. Reads PDF from stdin, JSON to stdout.

No application settings/secrets/network or user-selected path is available.
"""
import json
import sys
from io import BytesIO


def main():
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (6, 6))
        if sys.platform.startswith("linux"):
            resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    except (ImportError, ValueError, OSError):
        pass
    try:
        from pypdf import PdfReader
        content = sys.stdin.buffer.read(5 * 1024 * 1024 + 1)
        if len(content) > 5 * 1024 * 1024 or not content.startswith(b"%PDF-"):
            raise ValueError()
        reader = PdfReader(BytesIO(content), strict=True)
        if reader.is_encrypted or len(reader.pages) > 30:
            raise ValueError()
        chunks = []
        total = 0
        for page in reader.pages:
            chunk = (page.extract_text() or "")[:12000]
            chunks.append(chunk)
            total += len(chunk)
            if total >= 12000:
                break
        text = "\n".join(chunks)[:12000]
        if len(text.strip()) < 10:
            raise ValueError()
        sys.stdout.write(json.dumps({"text": text, "pages": len(reader.pages)}, ensure_ascii=False))
    except Exception:
        sys.stdout.write('{"error":"invalid_pdf"}')
        sys.exit(1)


if __name__ == "__main__":
    main()
