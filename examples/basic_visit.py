"""Basic SPECTER visit — observational goal, no LLM, no vision.

This is the smallest possible end-to-end demo. It visits a staging
endpoint, takes a screenshot, and prints the page title. No actions,
no provider detection challenges triggered (mock `/none` is unprotected).

Run:
    python -m specter --serve-mock &      # start the staging server
    python examples/basic_visit.py         # in another shell
"""

import os
import sys
from pathlib import Path

# add the project root so the specter package + cf_selenium are importable
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from specter import AIWebAgent


def main() -> int:
    with AIWebAgent(
        profile="example-basic",
        allowed_domains=["127.0.0.1"],
        authorized_test_mode=True,
        use_llm=False,       # rule-based, no router needed
        use_vision=False,    # no tesseract needed
    ) as agent:
        result = agent.run(
            url="http://127.0.0.1:18801/none",
            goal="what is the page title?",
        )
        print(f"success: {result.success}")
        print(f"final_url: {result.final_url}")
        print(f"summary: {result.summary}")
        print(f"artifacts: {result.artifacts_dir}")
        print(f"steps: {len(result.steps)}")
        return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
