# Automation: Weekly Social Media Locations

## Schedule
- **When:** Mondays at **8:00 AM America/New_York**
- **Repo:** `nmarquettej/Vet-Margin-Dashboard`
- **Branch:** prefer latest `cursor/weekly-social-locations-*` (or `main` once merged)

## Goal
Each Monday, produce a staff email that:
1. Lists that week’s active Shepherd appointments (Mon–Sun)
2. Highlights high-interest social stops near the route (murals, statues, landmarks)
3. Surfaces community / rescue / 501(c)(3)-friendly events overlapping those areas
4. Helps the team plan short photo/content stops during the week

Brand lens: community-oriented, family-owned, rescue/501(c)(3)-friendly.

## Cursor Automation prompt (paste into the automation)

```text
You are running the Whisker Waggin Monday social-location planner.

1. Ensure Shepherd + COMPOSIO_API_KEY secrets are available.
2. From the repo root, run:
   python3 scripts/weekly_social_locations_run.py --live --week-start TODAY
   (The script resolves the Mon–Sun week containing TODAY in America/New_York.)
3. If Google Calendar is connected in Composio, the script merges event locations.
   If Calendar is not connected, note that in your summary and continue — Shepherd
   OpenAPI does not expose client street addresses; use known partner orgs + area hints.
4. After the script succeeds, optionally enrich with 1–2 fresh Composio web/maps searches
   for any NEW towns that appeared on this week’s schedule and are missing from the email.
   If you find strong additional stops/events, send a short follow-up email to
   courtney@whiskerwagginwnc.com (CC nmarquettej@gmail.com) from the NoReply Gmail
   connection summarizing only the extras — do not invent a signature; use GMAIL_LIST_SEND_AS.
5. Do not email clients. Staff report only.
6. Reply with: week window, appointment count, areas, whether the staff email sent, and message id.
```

## Manual / dry-run commands

```bash
# Schedule JSON only
python3 scripts/weekly_social_locations_run.py --extract-only --week-start 2026-08-24

# Build HTML without sending
python3 scripts/weekly_social_locations_run.py --dry-run --week-start 2026-08-24

# Research + email Courtney (CC Nick)
python3 scripts/weekly_social_locations_run.py --live --week-start 2026-08-24
```

## Required secrets
- `SHEPHERD_PUBLIC_KEY`
- `SHEPHERD_PRIVATE_KEY`
- `SHEPHERD_CLINIC_ID`
- `SHEPHERD_BASE_URL`
- `COMPOSIO_API_KEY`

## Optional upgrade
Connect the Whisker Waggin **Google Calendar** that stores appointment/route locations in Composio so street addresses appear on the report (Shepherd OpenAPI currently omits them).
