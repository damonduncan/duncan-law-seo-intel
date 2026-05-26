import json


def dedup_review_snaps(snaps: list) -> list:
    """Deduplicate snapshots by snapshot_data fingerprint."""
    seen, out = set(), []
    for s in snaps:
        fp = json.dumps(s.snapshot_data, sort_keys=True) if s.snapshot_data else str(id(s))
        if fp not in seen:
            seen.add(fp)
            out.append(s)
    return out
