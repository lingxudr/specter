"""Quick Tesseract smoke test - generate image, OCR it, verify text."""
import sys
from PIL import Image, ImageDraw, ImageFont
import pytesseract

# Generate test image with known text
img = Image.new("RGB", (400, 100), "white")
draw = ImageDraw.Draw(img)
try:
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 30)
except Exception:
    font = ImageFont.load_default()

text = "Submit Button"
draw.text((20, 30), text, fill="black", font=font)
img.save("/tmp/tess_test.png")
print(f"image saved: /tmp/tess_test.png")

# OCR
out = pytesseract.image_to_string(Image.open("/tmp/tess_test.png")).strip()
print(f"OCR output: {out!r}")

# Word-level detail
data = pytesseract.image_to_data(Image.open("/tmp/tess_test.png"), output_type=pytesseract.Output.DICT)
print(f"word count: {len([w for w in data['text'] if w.strip()])}")
for i, w in enumerate(data["text"]):
    if w.strip():
        print(f"  word[{i}] = {w!r}  conf={data['conf'][i]}  bbox=({data['left'][i]},{data['top'][i]},{data['width'][i]},{data['height'][i]})")

# Confidence sanity
words = [w for w in data["text"] if w.strip()]
if words:
    confs = [int(data["conf"][i]) for i, w in enumerate(data["text"]) if w.strip()]
    print(f"avg conf: {sum(confs)/len(confs):.1f}")
    assert "Submit" in out or "Button" in out, f"OCR failed to read expected text: got {out!r}"
    print("PASS: Tesseract OCR works")
else:
    print("FAIL: no words detected")
    sys.exit(1)
