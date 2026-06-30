from __future__ import annotations

import logging
from datetime import datetime, timezone

from ci_inspector.core.models import DiscoveryResult

logger = logging.getLogger(__name__)

TOOL_COLUMNS = [
    "CI Name", "Stage", "Env Type", "Components",
    "vCPUs", "Memory (GB)", "Storage (GB)", "Cost/hr", "Last Updated",
]

CLOUD_SHEETS = ["AWS", "Azure", "GCP", "CNV"]


def build_row(result: DiscoveryResult) -> list:
    components_str = ", ".join(
        f"{c.count}x {c.purpose} ({c.instance_type})" for c in result.components
    )
    cost = f"${result.total_cost_per_hour}" if result.total_cost_per_hour else ""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

    return [
        result.ci_name,
        result.stage,
        result.env_type or "",
        components_str,
        result.total_vcpus,
        result.total_memory_gb,
        sum(c.storage_gb * c.count for c in result.components),
        cost,
        now,
    ]


def sync_to_sheets(
    results: list[DiscoveryResult],
    sheet_id: str,
    credentials_path: str,
    prune: bool = False,
) -> None:
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError as e:
        raise ImportError(
            "gspread and google-auth are required for sheets output. "
            "Install with: pip install gspread google-auth"
        ) from e

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(credentials_path, scopes=scopes)
    gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_key(sheet_id)

    # Group results by cloud
    by_cloud: dict[str, list[DiscoveryResult]] = {}
    for r in results:
        cloud = r.cloud_provider or "Unknown"
        by_cloud.setdefault(cloud, []).append(r)

    # Sync each cloud worksheet
    for cloud in CLOUD_SHEETS:
        cloud_results = by_cloud.get(cloud, [])
        try:
            ws = spreadsheet.worksheet(cloud)
        except gspread.WorksheetNotFound:
            ws = spreadsheet.add_worksheet(title=cloud, rows=1000, cols=20)
            ws.append_row(TOOL_COLUMNS)

        existing = ws.get_all_values()
        rows = existing[1:] if len(existing) > 1 else []

        # Build lookup: (ci_name, stage) -> row_index (1-based, header is row 1)
        existing_keys: dict[tuple[str, str], int] = {}
        for idx, row in enumerate(rows):
            if len(row) >= 2:
                key = (row[0].replace("(removed) ", ""), row[1])
                existing_keys[key] = idx + 2  # 1-based + header row

        seen_keys: set[tuple[str, str]] = set()
        for r in cloud_results:
            key = (r.ci_name, r.stage)
            seen_keys.add(key)
            new_row = build_row(r)

            if key in existing_keys:
                row_num = existing_keys[key]
                # Update only tool-owned columns (first N columns)
                for col_idx, val in enumerate(new_row):
                    ws.update_cell(row_num, col_idx + 1, val)
            else:
                ws.append_row(new_row)

        # Mark removed CIs
        for key, row_num in existing_keys.items():
            if key not in seen_keys:
                current_name = ws.cell(row_num, 1).value or ""
                if not current_name.startswith("(removed)"):
                    if prune:
                        ws.delete_rows(row_num)
                    else:
                        ws.update_cell(row_num, 1, f"(removed) {current_name}")

    # Summary sheet
    try:
        summary = spreadsheet.worksheet("All CIs")
    except gspread.WorksheetNotFound:
        summary = spreadsheet.add_worksheet(title="All CIs", rows=1000, cols=20)
        summary.append_row(TOOL_COLUMNS + ["Cloud"])

    # Rebuild summary from all results
    summary.clear()
    summary.append_row(TOOL_COLUMNS + ["Cloud"])
    for r in results:
        row = build_row(r) + [r.cloud_provider or "Unknown"]
        summary.append_row(row)

    logger.info("Synced %d CIs to sheet %s", len(results), sheet_id)
