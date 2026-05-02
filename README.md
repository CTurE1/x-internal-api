# x-internal-api

Auto-refreshed catalog of X.com GraphQL operations (queryIds, features, field toggles) captured from the live web bundle.

## Why

Twitter rotates GraphQL queryIds without notice. Apps using stale hashes silently break. This repo refreshes the manifest every 6 hours via GitHub Actions and publishes it as a single JSON file consumable by any ShadowX-based project.

## URL

`https://raw.githubusercontent.com/CTurE1/x-internal-api/main/data/graphql_ops.json`

## Schema

```json
{
  "captured_at": "2026-05-02T11:00:00+00:00",
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

## Update cadence

GitHub Actions cron: every 6 hours. Manual refresh via the `Actions` tab → `refresh` → `Run workflow`.

## Manual run

```bash
pip install -r requirements.txt
python scripts/capture_ops.py
```
