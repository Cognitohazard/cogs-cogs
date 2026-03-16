# RemindConfirm

A [Red-Bot](https://github.com/Cog-Creators/Red-DiscordBot) cog that sends scheduled reminders and nags mentioned users until they confirm with a reaction emoji.

## What it does

You set up a reminder with a schedule, a message, and a list of users. When it fires, the bot posts the message and adds a reaction emoji (default ✅). It keeps re-posting at a configurable interval until every listed user has reacted, or the nag window expires. Confirmation state resets each occurrence.

Supports M-of-N confirmations — require only 2 out of 5 mentioned users to confirm, for example.

## Requirements

- Red-Bot 3.5.0+
- Python 3.7+

## Installation

```
[p]repo add cogs-cogs <repo_url>
[p]cog install cogs-cogs remindconfirm
[p]load remindconfirm
```

## Commands

All commands live under `[p]rc` (alias for `[p]remindconfirm`). Creating and managing reminders requires the **Manage Server** permission.

### Set timezone

```
[p]rc timezone America/Chicago
```

Uses IANA timezone names. Defaults to `America/New_York`. All absolute times are interpreted in this timezone.

### Interval reminder

Fires on a fixed interval (e.g. every 3 days).

```
[p]rc interval <first_fire> <interval> <nag_interval> <nag_expiry> "<message>" @user1 @user2 [required_count]
```

**Example** — every 2 days starting in 1 hour, nag every 30 minutes for up to 8 hours:

```
[p]rc interval "in 1h" 2d 30m 8h "Take out the trash" @Alice @Bob
```

### Weekly reminder

Fires on specific days of the week at a set time.

```
[p]rc weekly <days> <time> <nag_interval> <nag_expiry> "<message>" @user1 @user2 [required_count]
```

**Example** — every Monday and Thursday at 9:00 AM, nag hourly for 8 hours:

```
[p]rc weekly mon,thu 09:00 1h 8h "Submit weekly report" @Alice @Bob
```

### One-shot reminder

Fires once. Waits 24 hours for confirmations, no repeated nags.

```
[p]rc once <when> "<message>" @user1 @user2 [required_count]
```

### One-shot with nag

Same as above but re-posts at an interval until confirmed or expired.

```
[p]rc once_nag <when> <nag_interval> <nag_expiry> "<message>" @user1 @user2 [required_count]
```

### M-of-N confirmations

Any creation command accepts an optional `required_count` as the last argument. If set, the reminder resolves once that many users have confirmed, rather than requiring all of them.

```
[p]rc once "in 1h" "Someone water the plants" @Alice @Bob @Carol 1
```

### Other commands

| Command | Description |
|-|-|
| `[p]rc list` | Show all active reminders with confirmation status |
| `[p]rc cancel <id>` | Cancel a reminder |
| `[p]rc emoji <id> <emoji>` | Change the confirmation emoji |
| `[p]rc timezone [tz]` | View or set server timezone |

## Time formats

- **Relative:** `in 2h`, `in 30m`, `in 1d`
- **Absolute:** `2024-03-20T14:30` (interpreted in server timezone)
- **Durations:** `30m`, `2h`, `3d`, `1w`, `1d12h` (used for intervals and nag settings)
- **Days:** `monday`, `mon`, `tue,thu,sat` (comma-separated)

## How it works

Each reminder runs as an async background task. When a reminder fires, the bot posts the message, adds the confirmation emoji, and starts a nag loop. On each nag tick, it deletes the previous message and re-posts so the reminder stays visible. When a user reacts with the correct emoji, their confirmation is recorded. Once enough users confirm, the occurrence ends and the reminder either reschedules (interval/weekly) or deactivates (one-shot).

All state is persisted through Red-Bot's Config system and survives bot restarts.

## Data storage

This cog stores user IDs for reminder targets and confirmation tracking. No message content is stored beyond the reminder text configured by server admins.
