"""specter CLI entry point.

Run with:
    python -m specter <url> <goal> [options]
    python -m specter --task task.json
    python -m specter --serve-mock [--port 18801]
"""

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser(
        prog="specter",
        description="SPECTER — staging-only AI web-agent for authorized security testing",
    )
    p.add_argument("url", nargs="?", help="target URL (staging only)")
    p.add_argument("goal", nargs="?", help="what the agent should do / observe")
    p.add_argument("--task", help="path to a JSON task file")
    p.add_argument("--max-steps", type=int, default=10)
    p.add_argument("--profile", default="specter")
    p.add_argument("--no-llm", action="store_true", help="rule-based only")
    p.add_argument("--use-vision", action="store_true", help="enable vision decision layer")
    p.add_argument("--allow", dest="allowed_domains", help="comma-separated allowed domains")
    p.add_argument("--authorized-test-mode", action="store_true", default=True)
    p.add_argument("--serve-mock", action="store_true", help="run the staging mock server")
    p.add_argument("--port", type=int, default=18801, help="mock server port")
    p.add_argument("--version", action="version", version="%(prog)s 0.5.0")
    args = p.parse_args()

    if args.serve_mock:
        # delegate to mock_server
        sys.path.insert(0, str(Path(__file__).parent))
        sys.argv = [str(Path(__file__).parent / "mock_server.py"), "--port", str(args.port)]
        from specter import mock_server
        return mock_server.main()

    from specter.agent import AIWebAgent

    if args.task:
        task = json.loads(Path(args.task).read_text())
        url = task["url"]
        goal = task.get("goal", "")
        max_steps = int(task.get("max_steps", args.max_steps))
        use_llm = bool(task.get("use_llm", not args.no_llm))
        use_vision = bool(task.get("use_vision", args.use_vision))
    else:
        if not args.url:
            p.error("url required (or use --task / --serve-mock)")
        url, goal = args.url, args.goal or "describe the page"
        max_steps = args.max_steps
        use_llm = not args.no_llm
        use_vision = args.use_vision

    allowed = []
    if args.allowed_domains:
        allowed = [d.strip() for d in args.allowed_domains.split(",")]

    with AIWebAgent(
        profile=args.profile,
        allowed_domains=allowed,
        authorized_test_mode=args.authorized_test_mode,
        use_llm=use_llm,
        use_vision=use_vision,
        max_steps=max_steps,
    ) as agent:
        result = agent.run(url, goal=goal)
        print(json.dumps({
            "success": result.success,
            "final_url": result.final_url,
            "summary": result.summary,
            "steps": result.steps,
            "error": result.error,
        }, indent=2, default=str))
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
