"""Command-line redaction of a local PDF, no web UI needed.

    python -m app.try_redact <input.pdf> [auto|text|ocr] [Category ...]
    python -m app.try_redact samples/sample-text.pdf auto Person Address DateOfBirth

Writes <input>-redacted.pdf next to the input (never overwrites: adds -2, -3, ...).
"""
import asyncio
import json
import sys
from pathlib import Path

from .redaction import redact_pdf


async def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    source = Path(sys.argv[1])
    mode = sys.argv[2] if len(sys.argv) > 2 else "auto"
    categories = sys.argv[3:] or ["Person", "Address", "DateOfBirth"]
    result = await redact_pdf(source.read_bytes(), categories, mode=mode)
    target, n = source.with_name(f"{source.stem}-redacted.pdf"), 1
    while target.exists():
        n += 1
        target = source.with_name(f"{source.stem}-redacted-{n}.pdf")
    target.write_bytes(result["pdf"])
    print(json.dumps({"method": result["method"], "counts": result["counts"], "output": str(target)}, indent=2))


asyncio.run(main())
