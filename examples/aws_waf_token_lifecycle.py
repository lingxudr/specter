"""AWS WAF token lifecycle — full walkthrough.

Demonstrates the four operations every operator will perform:

    1. STORE a token (you obtained it out-of-band; this is legit)
    2. ATTACH it to the session
    3. CHECK state (has_usable, needs_refresh, etc.)
    4. INVALIDATE on 401/403/logout

Run:
    python -m specter --serve-mock &
    python examples/aws_waf_token_lifecycle.py

Expected output: all four steps print structured state. The session
file `aws_waf_lifecycle.db` and the token store file
`aws_waf_lifecycle_tokens.json` are written to the current dir so
you can inspect them.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from specter.providers import AWSWAFAdapter, AWSWAFTokenStore
from specter.sessions import BypassSession


def main() -> int:
    host = "staging.example.com"
    db_path = Path("aws_waf_lifecycle.db")
    store_path = Path("aws_waf_lifecycle_tokens.json")

    store = AWSWAFTokenStore(path=store_path)
    adapter = AWSWAFAdapter(store=store)
    bs = BypassSession(db_path=db_path)

    # 1. STORE — out-of-band token, you got it from a solved challenge
    print("--- 1. STORE ---")
    adapter.store_token(
        value="eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.example",
        host=host,
        max_age=3600,
        source="manual",
        notes="obtained from staging partner integration",
    )
    print(f"stored: {adapter.token_state(host)['token']['value'][:20]}...")

    # 2. ATTACH to the session
    print("\n--- 2. ATTACH ---")
    bs.apply_aws_waf_token(
        host, "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.example",
        expires_in=3600, source="manual",
    )
    print(f"session has token: {bs.has_aws_waf_token(host)}")
    sess_tok = bs.get_aws_waf_token(host)
    print(f"session token: {sess_tok['value'][:20] if sess_tok else None}...")

    # 3. CHECK state
    print("\n--- 3. CHECK ---")
    state = adapter.token_state(host)
    for k, v in state.items():
        if k == "token" and v:
            v = f"<token {v['value'][:20]}... expires={v['expires_at']}>"
        print(f"  {k}: {v}")

    # 4. INVALIDATE on logout
    print("\n--- 4. INVALIDATE ---")
    adapter.invalidate_token(host, reason="logout")
    print(f"after invalidate: has_usable={adapter.token_state(host)['has_usable_token']}")
    print(f"session after invalidate: {bs.get_aws_waf_token(host)}")

    # 5. REFRESH (rotate) — get a new token out-of-band, store it
    print("\n--- 5. REFRESH (rotate) ---")
    bs.refresh_aws_waf_token(host)  # marks the existing token for rotation
    adapter.store_token(
        value="eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.rotated",
        host=host, max_age=3600, source="manual", notes="rotated v2",
    )
    bs.apply_aws_waf_token(
        host, "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.rotated",
        expires_in=3600, source="manual",
    )
    state = adapter.token_state(host)
    print(f"after refresh: has_usable={state['has_usable_token']}, "
          f"total_stored={state['total_stored']}, "
          f"invalidated_count={state['invalidated_count']}")

    print("\n--- done ---")
    print(f"session db: {db_path.absolute()}")
    print(f"token store: {store_path.absolute()}")
    print("inspect with `sqlite3 aws_waf_lifecycle.db .dump` and `cat aws_waf_lifecycle_tokens.json`")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
