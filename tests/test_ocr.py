"""Test tesseract OCR pipeline inside proot ubuntu."""
from PIL import Image, ImageDraw, ImageFont
import subprocess
import os

# Create a synthetic test image with text
img = Image.new("RGB", (400, 100), "white")
draw = ImageDraw.Draw(img)
# Try a font, fallback to default
try:
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 36)
except Exception:
    font = ImageFont.load_default()

draw.text((20, 20), "Verify you are human", fill="black", font=font)
img_path = "/data/data/com.termux/files/home/test_ocr.png"
img.save(img_path)
print(f"image: {img_path} size={os.path.getsize(img_path)}B")

# Run tesseract directly via subprocess
out = subprocess.run(
    ["tesseract", img_path, "-", "--psm", "6"],
    capture_output=True, text=True, timeout=30
)
print(f"stdout: {out.stdout.strip()!r}")
print(f"stderr: {out.stderr.strip()!r}")
print(f"exit:   {out.returncode}")
