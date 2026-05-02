# x-internal-api

Auto-refreshed catalog of X.com GraphQL operations (queryIds, features, field toggles) captured from the live web bundle.

## Why

Twitter rotates GraphQL queryIds without notice. Apps using stale hashes silently break. This repo refreshes the manifest every 6 hours via GitHub Actions and publishes it as a single JSON file consumable by any ShadowX-based project.

## URL

`https://raw.githubusercontent.com/CTurE1/x-internal-api/main/data/graphql_ops.json`

## Schema

`graphql_ops.json` — canonical, byte-stable when ops don't change:

```json
{
  "ops_count": 154,
  "ops": {
    "TweetDetail": {
      "queryId": "B3ZxDi__9OXTkCCuAp79w",
      "operationName": "TweetDetail",
      "operationType": "query",
      "method": "GET",
      "featureSwitches": ["..."],
      "fieldToggles": ["..."]
    }
  }
}
```

`captured_at` is intentionally **not** in this file — it would change every 6h and trigger noise commits + false drift alerts. Use the GitHub commit timestamp on `data/graphql_ops.json` (`git log data/graphql_ops.json`) as the canonical "fresh as of" signal.

## Update cadence

GitHub Actions cron: every 6 hours. Manual refresh via the `Actions` tab → `refresh` → `Run workflow`.

When ops drift is detected, the workflow:
1. Validates the new capture (schema + minimum op count + required ops present)
2. Commits `data/graphql_ops.json`
3. Sends a Telegram alert listing every queryId / method change

## Consume in code

Python:

```python
import httpx, functools

OPS_URL = "https://raw.githubusercontent.com/CTurE1/x-internal-api/main/data/graphql_ops.json"

@functools.lru_cache(maxsize=1)
def get_ops() -> dict:
    """Fetch fresh ops, cached for the process lifetime."""
    r = httpx.get(OPS_URL, timeout=5.0)
    r.raise_for_status()
    return r.json()["ops"]

ops = get_ops()
tweet_detail = ops["TweetDetail"]
print(tweet_detail["queryId"])  # always fresh (≤6h old)
```

Always pair the remote fetch with a local fallback: if the URL is unreachable at startup, fall back to a bundled snapshot you ship with your app. Don't run live without a fallback.

## Manual run

```bash
pip install -r requirements.txt
python scripts/capture_ops.py
python scripts/validate_ops.py
```

## License

This catalog is captured from publicly-available content served by x.com. The capture mechanism + JSON shape are released to the public domain (Unlicense).
