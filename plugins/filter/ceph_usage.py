# Usage-vs-quota arithmetic for the rgw_tenants report. Pure; it runs on the control host
# against what rgw_account_info already read.


def _pct(used, cap):
    """Percent of a finite, positive cap. None for unlimited (-1/0) or a missing value."""
    try:
        used, cap = int(used), int(cap)
    except (TypeError, ValueError):
        return None
    if cap <= 0:
        return None
    return round(used * 100.0 / cap, 1)


def ceph_usage_report(accounts, tenants=None):
    """Join rgw_account_info accounts to the configured tenants and add the percentages.

    accounts is the module's `accounts` list; tenants is rgw_tenants_list, used only to
    label each row with its configured name. An account with no matching tenant entry
    keeps its own account name, so the filter also reports unmanaged accounts.
    """
    names = dict((t["account_id"], t.get("name", "")) for t in (tenants or []))
    rows = []
    for account in accounts or []:
        quota = account.get("quota") or {}
        stats = account.get("stats") or {}
        used_size = stats.get("size", 0)
        used_objects = stats.get("num_objects", 0)
        max_size = quota.get("max_size", -1)
        max_objects = quota.get("max_objects", -1)
        rows.append({
            "name": names.get(account["id"]) or account.get("name", "") or account["id"],
            "account_id": account["id"],
            "enabled": bool(quota.get("enabled", False)),
            "used_size": used_size,
            "max_size": max_size,
            "size_pct": _pct(used_size, max_size),
            "used_objects": used_objects,
            "max_objects": max_objects,
            "objects_pct": _pct(used_objects, max_objects),
        })
    return rows


def ceph_over_quota(rows, threshold=80):
    """Names from ceph_usage_report at or over `threshold` percent of either cap."""
    return sorted({row["name"] for row in rows or []
                   if (row.get("size_pct") is not None and row["size_pct"] >= threshold)
                   or (row.get("objects_pct") is not None
                       and row["objects_pct"] >= threshold)})


class FilterModule:
    """Ceph RGW usage-report filters."""

    def filters(self):
        return {
            "ceph_usage_report": ceph_usage_report,
            "ceph_over_quota": ceph_over_quota,
        }
