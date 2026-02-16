"""RemindConfirm – recurring confirmation reminders for Red-Bot."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set

import discord
from redbot.core import Config, commands
from redbot.core.bot import Red

log = logging.getLogger("red.remindconfirm")

# ── Time-parsing utilities ──────────────────────────────────────────────

DURATION_RE = re.compile(
    r"^(?:(\d+)w)?(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?$", re.IGNORECASE
)

DAY_NAMES = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

DAY_ABBREVS = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}


def parse_duration(text: str) -> Optional[timedelta]:
    """Parse a duration string like ``2h30m``, ``1d``, ``1w`` into a timedelta."""
    m = DURATION_RE.match(text.strip())
    if not m:
        return None
    weeks = int(m.group(1) or 0)
    days = int(m.group(2) or 0)
    hours = int(m.group(3) or 0)
    minutes = int(m.group(4) or 0)
    if weeks == days == hours == minutes == 0:
        return None
    return timedelta(weeks=weeks, days=days, hours=hours, minutes=minutes)


def parse_fire_time(text: str) -> Optional[datetime]:
    """Parse an absolute ISO-8601 datetime **or** a relative offset (``in 2h``)."""
    text = text.strip()
    # Relative: "in 2h", "in 30m", "in 1d"
    if text.lower().startswith("in "):
        delta = parse_duration(text[3:])
        if delta is None:
            return None
        return datetime.now(timezone.utc) + delta
    # Absolute ISO
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def parse_weekdays(text: str) -> Optional[List[int]]:
    """Parse comma-separated day names into weekday ints (0=Mon … 6=Sun)."""
    days: List[int] = []
    for part in text.lower().split(","):
        part = part.strip()
        if part not in DAY_NAMES:
            return None
        day = DAY_NAMES[part]
        if day not in days:
            days.append(day)
    return sorted(days) if days else None


def parse_time_of_day(text: str) -> Optional[tuple]:
    """Parse ``HH:MM`` into (hour, minute)."""
    m = re.match(r"^(\d{1,2}):(\d{2})$", text.strip())
    if not m:
        return None
    h, mn = int(m.group(1)), int(m.group(2))
    if 0 <= h <= 23 and 0 <= mn <= 59:
        return (h, mn)
    return None


def next_weekly_fire(weekdays: List[int], hour: int, minute: int) -> datetime:
    """Return the next datetime matching one of the given weekdays at HH:MM (UTC)."""
    now = datetime.now(timezone.utc)
    for offset in range(8):
        candidate = now + timedelta(days=offset)
        if candidate.weekday() in weekdays:
            fire = candidate.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if fire > now:
                return fire
    # Fallback (shouldn't happen with range(8))
    return now + timedelta(days=1)


def _pending_users(rdata: dict) -> List[int]:
    """Return user IDs from required_users that are not yet in confirmed_users."""
    confirmed: Set[int] = set(rdata["confirmed_users"])
    return [uid for uid in rdata["required_users"] if uid not in confirmed]


def _all_confirmed(rdata: dict) -> bool:
    """Check whether every required user has confirmed."""
    return set(rdata["required_users"]).issubset(rdata["confirmed_users"])


def _pending_mentions(rdata: dict) -> str:
    """Return a comma-separated string of mentions for users who haven't confirmed."""
    return ", ".join(f"<@{uid}>" for uid in _pending_users(rdata))


def _confirmed_mentions(rdata: dict) -> str:
    """Return a comma-separated string of mentions for users who have confirmed."""
    return ", ".join(f"<@{uid}>" for uid in rdata["confirmed_users"])


def _format_schedule(rdata: dict) -> str:
    """Human-readable schedule string for display."""
    if rdata["schedule_type"] == "interval":
        return f"Every `{rdata['schedule_interval']}`"
    elif rdata["schedule_type"] == "weekly":
        days_display = ", ".join(DAY_ABBREVS[d] for d in rdata.get("weekdays", []))
        return f"**{days_display}** at **{rdata.get('fire_time', '??')}** UTC"
    return "Unknown"


# ── Reminder data factory ───────────────────────────────────────────────


def _make_reminder(
    *,
    rid: str,
    channel_id: int,
    message: str,
    schedule_type: str,
    nag_interval: str,
    nag_expiry: str,
    next_fire_at: datetime,
    required_users: List[int],
    creator_id: int,
    schedule_interval: Optional[str] = None,
    weekdays: Optional[List[int]] = None,
    fire_time: Optional[str] = None,
) -> dict:
    """Create a canonical reminder dict with all required fields."""
    return {
        "reminder_id": rid,
        "channel_id": channel_id,
        "message": message,
        "schedule_type": schedule_type,
        "schedule_interval": schedule_interval,
        "weekdays": weekdays,
        "fire_time": fire_time,
        "nag_interval": nag_interval,
        "nag_expiry": nag_expiry,
        "next_fire_at": next_fire_at.isoformat(),
        "required_users": required_users,
        "confirmed_users": [],
        "current_message_id": None,
        "occurrence_started_at": None,
        "creator_id": creator_id,
        "emoji": "✅",
        "active": True,
    }


# ── Cog ─────────────────────────────────────────────────────────────────


class RemindConfirm(commands.Cog):
    """Recurring reminders that require reaction confirmations from specified users."""

    DEFAULT_GUILD = {"reminders": {}}

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=298537412, force_registration=True)
        self.config.register_guild(**self.DEFAULT_GUILD)
        self._tasks: Dict[str, asyncio.Task] = {}
        # In-memory index: message_id → (guild_id, reminder_id)
        # Avoids scanning all reminders on every reaction event.
        self._msg_index: Dict[int, tuple] = {}

    async def cog_load(self) -> None:
        """Re-schedule all active reminders on bot restart / cog load."""
        all_guilds = await self.config.all_guilds()
        for guild_id, guild_data in all_guilds.items():
            for rid, rdata in guild_data.get("reminders", {}).items():
                if rdata.get("active", False):
                    self._schedule_task(guild_id, rid, rdata)
                    if rdata.get("current_message_id"):
                        self._msg_index[rdata["current_message_id"]] = (guild_id, rid)

    async def cog_unload(self) -> None:
        """Cancel all running tasks on cog unload."""
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()
        self._msg_index.clear()

    # ── Helpers ──────────────────────────────────────────────────────────

    def _schedule_task(self, guild_id: int, reminder_id: str, rdata: dict) -> None:
        """Create and store an asyncio task for a reminder."""
        key = f"{guild_id}:{reminder_id}"
        existing = self._tasks.get(key)
        if existing is not None and not existing.done():
            existing.cancel()
        self._tasks[key] = asyncio.create_task(
            self._reminder_loop(guild_id, reminder_id),
            name=f"remindconfirm-{key}",
        )

    async def _get_reminder(self, guild_id: int, reminder_id: str) -> Optional[dict]:
        """Fetch a single reminder dict from config."""
        reminders = await self.config.guild_from_id(guild_id).reminders()
        return reminders.get(reminder_id)

    async def _save_reminder(self, guild_id: int, reminder_id: str, data: dict) -> None:
        """Persist a single reminder dict into config."""
        async with self.config.guild_from_id(guild_id).reminders() as reminders:
            reminders[reminder_id] = data

    def _build_status_embed(self, rdata: dict) -> discord.Embed:
        """Build the recurring nag embed showing confirmation progress."""
        pending = _pending_users(rdata)
        total = len(rdata["required_users"])
        done = total - len(pending)

        embed = discord.Embed(
            title="⏰ Reminder",
            description=rdata["message"],
            colour=discord.Colour.orange() if pending else discord.Colour.green(),
        )

        if pending:
            embed.add_field(
                name=f"{len(pending)}/{total} confirmations still needed",
                value=f"Waiting on: {_pending_mentions(rdata)}",
                inline=False,
            )
        else:
            embed.add_field(
                name="✅ All confirmed!",
                value="Everyone has confirmed.",
                inline=False,
            )

        if rdata["confirmed_users"]:
            embed.add_field(
                name="Confirmed",
                value=_confirmed_mentions(rdata),
                inline=False,
            )

        embed.set_footer(text=f"ID: {rdata['reminder_id']}  •  React {rdata['emoji']} to confirm")
        return embed

    def _build_completion_embed(self, rdata: dict) -> discord.Embed:
        """Build the embed shown when all users have confirmed."""
        embed = discord.Embed(
            title="✅ Reminder — All confirmed!",
            description=rdata["message"],
            colour=discord.Colour.green(),
        )
        embed.add_field(name="Confirmed by", value=_confirmed_mentions(rdata), inline=False)
        embed.set_footer(text=f"ID: {rdata['reminder_id']}")
        return embed

    def _build_expiry_embed(self, rdata: dict) -> discord.Embed:
        """Build the embed shown when the nag window expires without full confirmation."""
        embed = discord.Embed(
            title="⏰ Reminder — nag window expired",
            description=rdata["message"],
            colour=discord.Colour.red(),
        )
        embed.add_field(
            name="Still not confirmed by",
            value=_pending_mentions(rdata),
            inline=False,
        )
        embed.set_footer(text=f"ID: {rdata['reminder_id']}  •  Will fire again next cycle")
        return embed

    def _build_creation_embed(self, rdata: dict, fire_dt: datetime, users: list) -> discord.Embed:
        """Build the embed shown when a reminder is first created."""
        is_weekly = rdata["schedule_type"] == "weekly"
        embed = discord.Embed(
            title=f"📋 {'Weekly reminder' if is_weekly else 'Reminder'} created",
            colour=discord.Colour.blurple(),
        )
        embed.add_field(name="ID", value=f"`{rdata['reminder_id']}`", inline=True)
        embed.add_field(name="Message", value=rdata["message"], inline=False)
        embed.add_field(name="Schedule", value=_format_schedule(rdata), inline=True)
        embed.add_field(name="First fire", value=f"<t:{int(fire_dt.timestamp())}:F>", inline=True)
        embed.add_field(
            name="Nag",
            value=f"Every `{rdata['nag_interval']}` for up to `{rdata['nag_expiry']}`",
            inline=True,
        )
        embed.add_field(
            name="Requires",
            value=", ".join(u.mention for u in users),
            inline=False,
        )
        return embed

    # ── Core loop ────────────────────────────────────────────────────────

    async def _reminder_loop(self, guild_id: int, reminder_id: str) -> None:
        """Main loop: wait for fire time → run nag occurrence → schedule next."""
        try:
            while True:
                rdata = await self._get_reminder(guild_id, reminder_id)
                if rdata is None or not rdata.get("active", False):
                    break

                # Wait until next fire
                fire_at = datetime.fromisoformat(rdata["next_fire_at"])
                now = datetime.now(timezone.utc)
                delay = (fire_at - now).total_seconds()
                if delay > 0:
                    await asyncio.sleep(delay)

                # Re-fetch in case cancelled during sleep
                rdata = await self._get_reminder(guild_id, reminder_id)
                if rdata is None or not rdata.get("active", False):
                    break

                # Run occurrence
                await self._run_occurrence(guild_id, reminder_id)

                # Re-fetch after occurrence (may have been cancelled)
                rdata = await self._get_reminder(guild_id, reminder_id)
                if rdata is None or not rdata.get("active", False):
                    break

                # Schedule next fire
                next_fire = self._compute_next_fire(rdata)
                rdata["next_fire_at"] = next_fire.isoformat()
                rdata["confirmed_users"] = []
                rdata["current_message_id"] = None
                rdata["occurrence_started_at"] = None
                await self._save_reminder(guild_id, reminder_id, rdata)

        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Error in reminder loop for %s in guild %s", reminder_id, guild_id)

    async def _run_occurrence(self, guild_id: int, reminder_id: str) -> None:
        """Nag loop for a single occurrence. Stops on all-confirmed or expiry."""
        rdata = await self._get_reminder(guild_id, reminder_id)
        if rdata is None or not rdata.get("active", False):
            return

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        channel = guild.get_channel(rdata["channel_id"])
        if channel is None:
            return

        nag_interval = parse_duration(rdata["nag_interval"])
        nag_expiry = parse_duration(rdata["nag_expiry"])
        if nag_interval is None or nag_expiry is None:
            log.error("Invalid nag_interval or nag_expiry for reminder %s", reminder_id)
            return

        occurrence_start = datetime.now(timezone.utc)
        rdata["occurrence_started_at"] = occurrence_start.isoformat()
        rdata["confirmed_users"] = []
        await self._save_reminder(guild_id, reminder_id, rdata)

        first_iteration = True
        while True:
            # On first iteration we send immediately; on subsequent ones we sleep first
            if not first_iteration:
                remaining_expiry = nag_expiry - (datetime.now(timezone.utc) - occurrence_start)
                sleep_time = min(nag_interval, remaining_expiry)
                sleep_secs = max(sleep_time.total_seconds(), 0)
                if sleep_secs > 0:
                    await asyncio.sleep(sleep_secs)
            first_iteration = False

            # Re-fetch state (reactions may have updated confirmed_users)
            rdata = await self._get_reminder(guild_id, reminder_id)
            if rdata is None or not rdata.get("active", False):
                return

            # Check all confirmed
            if _all_confirmed(rdata):
                await self._try_send(channel, embed=self._build_completion_embed(rdata))
                return

            # Check nag expiry
            elapsed = datetime.now(timezone.utc) - occurrence_start
            if elapsed >= nag_expiry:
                if _pending_users(rdata):
                    await self._try_send(channel, embed=self._build_expiry_embed(rdata))
                return

            # Delete old message and remove from index
            old_msg_id = rdata.get("current_message_id")
            if old_msg_id:
                self._msg_index.pop(old_msg_id, None)
                try:
                    old_msg = await channel.fetch_message(old_msg_id)
                    await old_msg.delete()
                except (discord.NotFound, discord.HTTPException):
                    pass

            # Send new reminder message (ping only pending users)
            embed = self._build_status_embed(rdata)
            content = " ".join(f"<@{uid}>" for uid in _pending_users(rdata))
            try:
                new_msg = await channel.send(content=content, embed=embed)
                await new_msg.add_reaction(rdata["emoji"])
            except discord.HTTPException:
                log.exception("Failed to send reminder message for %s", reminder_id)
                return

            # Update state and index
            rdata["current_message_id"] = new_msg.id
            self._msg_index[new_msg.id] = (guild_id, reminder_id)
            await self._save_reminder(guild_id, reminder_id, rdata)

    async def _try_send(self, channel: discord.abc.Messageable, **kwargs) -> None:
        """Send a message, swallowing HTTPExceptions."""
        try:
            await channel.send(**kwargs)
        except discord.HTTPException:
            pass

    def _compute_next_fire(self, rdata: dict) -> datetime:
        """Compute the next fire time based on schedule type."""
        now = datetime.now(timezone.utc)

        if rdata["schedule_type"] == "interval":
            interval = parse_duration(rdata["schedule_interval"])
            if interval is None:
                return now + timedelta(hours=1)  # fallback
            return now + interval

        elif rdata["schedule_type"] == "weekly":
            weekdays = rdata.get("weekdays", [])
            fire_time = rdata.get("fire_time", "12:00")
            parsed = parse_time_of_day(fire_time)
            if parsed is None or not weekdays:
                return now + timedelta(days=1)  # fallback
            hour, minute = parsed
            return next_weekly_fire(weekdays, hour, minute)

        return now + timedelta(hours=1)  # fallback

    # ── Reaction listener ────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        """Track confirmations via reactions."""
        if payload.guild_id is None:
            return
        if payload.user_id == self.bot.user.id:
            return

        # O(1) lookup instead of scanning all reminders
        lookup = self._msg_index.get(payload.message_id)
        if lookup is None:
            return
        guild_id, rid = lookup

        rdata = await self._get_reminder(guild_id, rid)
        if rdata is None or not rdata.get("active", False):
            return

        # Validate emoji
        if str(payload.emoji) != rdata.get("emoji", "✅"):
            return

        # Validate user
        if payload.user_id not in rdata["required_users"]:
            return
        if payload.user_id in rdata["confirmed_users"]:
            return

        # Record confirmation
        rdata["confirmed_users"].append(payload.user_id)
        await self._save_reminder(guild_id, rid, rdata)

        # Update embed in-place
        guild = self.bot.get_guild(guild_id)
        channel = guild.get_channel(rdata["channel_id"]) if guild else None
        if channel is None:
            return

        try:
            msg = await channel.fetch_message(payload.message_id)
            await msg.edit(embed=self._build_status_embed(rdata))
        except discord.HTTPException:
            pass

        if _all_confirmed(rdata):
            log.info("All users confirmed for reminder %s — occurrence complete", rid)

    # ── Commands ─────────────────────────────────────────────────────────

    @commands.group(name="remindconfirm", aliases=["rc"], invoke_without_command=True)
    @commands.guild_only()
    async def rc(self, ctx: commands.Context):
        """Manage recurring confirmation reminders."""
        await ctx.send_help(ctx.command)

    @rc.command(name="interval")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def rc_interval(
        self,
        ctx: commands.Context,
        first_fire: str,
        schedule_interval: str,
        nag_interval: str,
        nag_expiry: str,
        message: str,
        users: commands.Greedy[discord.Member],
    ):
        """Create an interval-based reminder.

        **Arguments:**
        - `<first_fire>` — when to first fire: ISO datetime or relative (e.g. ``in 2h``)
        - `<schedule_interval>` — time between fires (e.g. ``3d``, ``1w``)
        - `<nag_interval>` — re-send frequency within each occurrence (e.g. ``1h``)
        - `<nag_expiry>` — max nag window per occurrence (e.g. ``24h``)
        - `<message>` — the reminder text (use quotes)
        - `<@users>` — users who must confirm
        """
        if not users:
            return await ctx.send("❌ You must mention at least one user to confirm.")

        fire_dt = parse_fire_time(first_fire)
        if fire_dt is None:
            return await ctx.send('❌ Invalid first fire time. Use ISO format or `in 2h`.')

        if parse_duration(schedule_interval) is None:
            return await ctx.send("❌ Invalid schedule interval. Examples: `30m`, `2h`, `3d`, `1w`.")
        if parse_duration(nag_interval) is None:
            return await ctx.send("❌ Invalid nag interval. Examples: `30m`, `1h`.")
        if parse_duration(nag_expiry) is None:
            return await ctx.send("❌ Invalid nag expiry. Examples: `8h`, `24h`, `2d`.")

        rid = uuid.uuid4().hex[:8]
        rdata = _make_reminder(
            rid=rid,
            channel_id=ctx.channel.id,
            message=message,
            schedule_type="interval",
            schedule_interval=schedule_interval,
            nag_interval=nag_interval,
            nag_expiry=nag_expiry,
            next_fire_at=fire_dt,
            required_users=[u.id for u in users],
            creator_id=ctx.author.id,
        )

        await self._save_reminder(ctx.guild.id, rid, rdata)
        self._schedule_task(ctx.guild.id, rid, rdata)
        await ctx.send(embed=self._build_creation_embed(rdata, fire_dt, users))

    @rc.command(name="weekly")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def rc_weekly(
        self,
        ctx: commands.Context,
        days: str,
        time: str,
        nag_interval: str,
        nag_expiry: str,
        message: str,
        users: commands.Greedy[discord.Member],
    ):
        """Create a weekly reminder.

        **Arguments:**
        - `<days>` — day names, comma-separated (e.g. ``tuesday``, ``mon,wed,fri``)
        - `<time>` — time of day in 24h format (e.g. ``14:00``)
        - `<nag_interval>` — re-send frequency (e.g. ``1h``)
        - `<nag_expiry>` — max nag window (e.g. ``8h``)
        - `<message>` — the reminder text (use quotes)
        - `<@users>` — users who must confirm
        """
        if not users:
            return await ctx.send("❌ You must mention at least one user to confirm.")

        weekdays = parse_weekdays(days)
        if weekdays is None:
            return await ctx.send("❌ Invalid days. Examples: `tuesday`, `mon,wed,fri`.")

        parsed_time = parse_time_of_day(time)
        if parsed_time is None:
            return await ctx.send("❌ Invalid time. Use `HH:MM` (24h format).")

        if parse_duration(nag_interval) is None:
            return await ctx.send("❌ Invalid nag interval. Examples: `30m`, `1h`.")
        if parse_duration(nag_expiry) is None:
            return await ctx.send("❌ Invalid nag expiry. Examples: `8h`, `24h`, `2d`.")

        hour, minute = parsed_time
        fire_dt = next_weekly_fire(weekdays, hour, minute)

        rid = uuid.uuid4().hex[:8]
        rdata = _make_reminder(
            rid=rid,
            channel_id=ctx.channel.id,
            message=message,
            schedule_type="weekly",
            weekdays=weekdays,
            fire_time=time,
            nag_interval=nag_interval,
            nag_expiry=nag_expiry,
            next_fire_at=fire_dt,
            required_users=[u.id for u in users],
            creator_id=ctx.author.id,
        )

        await self._save_reminder(ctx.guild.id, rid, rdata)
        self._schedule_task(ctx.guild.id, rid, rdata)
        await ctx.send(embed=self._build_creation_embed(rdata, fire_dt, users))

    @rc.command(name="list")
    @commands.guild_only()
    async def rc_list(self, ctx: commands.Context):
        """List active reminders in this server."""
        reminders = await self.config.guild(ctx.guild).reminders()
        active = {rid: r for rid, r in reminders.items() if r.get("active", False)}

        if not active:
            return await ctx.send("No active reminders.")

        embed = discord.Embed(title="📋 Active reminders", colour=discord.Colour.blurple())
        for rid, r in active.items():
            pending = _pending_users(r)
            total = len(r["required_users"])

            next_fire = r.get("next_fire_at", "unknown")
            try:
                ts = int(datetime.fromisoformat(next_fire).timestamp())
                next_fire_display = f"<t:{ts}:R>"
            except (ValueError, TypeError):
                next_fire_display = next_fire

            value_lines = [
                f"**Message:** {r['message']}",
                f"**Schedule:** {_format_schedule(r)}",
                f"**Next fire:** {next_fire_display}",
                f"**Confirmations:** {total - len(pending)}/{total}",
            ]
            if pending:
                value_lines.append(f"**Waiting on:** {_pending_mentions(r)}")

            embed.add_field(name=f"`{rid}`", value="\n".join(value_lines), inline=False)

        await ctx.send(embed=embed)

    @rc.command(name="cancel")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def rc_cancel(self, ctx: commands.Context, reminder_id: str):
        """Cancel a reminder by ID."""
        rdata = await self._get_reminder(ctx.guild.id, reminder_id)
        if rdata is None:
            return await ctx.send(f"❌ No reminder found with ID `{reminder_id}`.")

        rdata["active"] = False
        await self._save_reminder(ctx.guild.id, reminder_id, rdata)

        # Cancel background task
        key = f"{ctx.guild.id}:{reminder_id}"
        task = self._tasks.pop(key, None)
        if task is not None:
            task.cancel()

        # Clean up message index and delete current message
        old_msg_id = rdata.get("current_message_id")
        if old_msg_id:
            self._msg_index.pop(old_msg_id, None)
            channel = ctx.guild.get_channel(rdata["channel_id"])
            if channel:
                try:
                    msg = await channel.fetch_message(old_msg_id)
                    await msg.delete()
                except (discord.NotFound, discord.HTTPException):
                    pass

        await ctx.send(f"✅ Reminder `{reminder_id}` cancelled.")

    @rc.command(name="emoji")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def rc_emoji(self, ctx: commands.Context, reminder_id: str, emoji: str):
        """Change the confirmation emoji for a reminder."""
        rdata = await self._get_reminder(ctx.guild.id, reminder_id)
        if rdata is None:
            return await ctx.send(f"❌ No reminder found with ID `{reminder_id}`.")

        rdata["emoji"] = emoji
        await self._save_reminder(ctx.guild.id, reminder_id, rdata)
        await ctx.send(f"✅ Emoji for `{reminder_id}` changed to {emoji}.")
