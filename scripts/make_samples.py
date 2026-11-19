"""Generate two FAKE test PDFs (no real PII):

  samples/sample-text.pdf     a normal text PDF      -> exercises the text (layout-preserving) route
  samples/sample-scanned.pdf  an image-only "scan"   -> exercises the OCR route

    python scripts/make_samples.py
"""
from pathlib import Path

import pymupdf

TEXT = """PATIENT INTAKE FORM

Patient name: Jane Q. Doe
Date of birth: March 14, 1985
Home address: 1234 Maple Avenue, Springfield, IL 62704
Phone: (217) 555-0142        Email: jane.doe@example.com
Employer: Contoso Manufacturing

Emergency contact: John Doe (spouse), phone (217) 555-0199

Notes: Patient Jane Doe was referred by Dr. Alan Smith on 05/02/2024 for a
follow-up visit. Insurance member ID on file.
"""

out_dir = Path(__file__).resolve().parent.parent / "samples"
out_dir.mkdir(exist_ok=True)

# 1) Text PDF
doc = pymupdf.open()
page = doc.new_page()
page.insert_textbox(pymupdf.Rect(60, 60, 550, 780), TEXT, fontsize=11, fontname="helv")
doc.save(out_dir / "sample-text.pdf")

# 2) "Scanned" PDF: render the page to an image and embed only the image (no text layer)
pix = page.get_pixmap(dpi=200, colorspace=pymupdf.csGRAY, alpha=False)
scan = pymupdf.open()
scan_page = scan.new_page(width=page.rect.width, height=page.rect.height)
scan_page.insert_image(scan_page.rect, stream=pix.tobytes("png"))
scan.save(out_dir / "sample-scanned.pdf")

print(f"Wrote {out_dir/'sample-text.pdf'} and {out_dir/'sample-scanned.pdf'}")
