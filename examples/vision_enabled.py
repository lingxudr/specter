"""Vision-enabled SPECTER visit.

Demonstrates the vision decision layer in action. The agent visits
the mock `/cloudflare` endpoint, the vision layer observes the page
via Tesseract (OCR) + DOM (HTML attribute regex), and reports a
verdict. The planner escalates to `wait` because the verdict
confidence exceeds 0.6 and the page is a known challenge.

Run:
    python -m specter --serve-mock &
    python examples/vision_enabled.py

Prereq:
    tesseract on PATH (apt install tesseract-ocr / pkg install tesseract)
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from specter import AIWebAgent


def main() -> int:
    with AIWebAgent(
        profile="example-vision",
        allowed_domains=["127.0.0.1"],
        authorized_test_mode=True,
        use_llm=False,       # keep it simple — vision alone
        use_vision=True,     # enable DOM + Tesseract
        max_steps=3,
    ) as agent:
        result = agent.run(
            url="http://127.0.0.1:18801/cloudflare",
            goal="describe the page",
        )
        print(f"success: {result.success}")
        print(f"final_url: {result.final_url}")
        print(f"summary: {result.summary}")

        # the agent keeps a vision_log on the run directory
        artifacts = Path(result.artifacts_dir) / "result.json"
        if artifacts.exists():
            import json
            data = json.loads(artifacts.read_text())
            vl = data.get("vision_log", [])
            print(f"\nvision_log entries: {len(vl)}")
            for i, v in enumerate(vl[:5]):
                print(f"  [{i}] hint={v.get('provider_hint')!r:25s} "
                      f"conf={v.get('confidence')} "
                      f"visible={v.get('challenge_visible')} "
                      f"sources={v.get('sources')}")
        return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
