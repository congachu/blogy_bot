# bot.py
import os
import re
import asyncio
import contextlib
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import asyncpg  # ← PostgreSQL async driver

# ========= 설정 =========
TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")  # eg. postgres://user:pass@host:5432/dbname
COMMAND_PREFIX = "!"
INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.members = True
BOT = commands.Bot(command_prefix=COMMAND_PREFIX, intents=INTENTS)

# 전역 풀 (on_ready에서 초기화)
PG_POOL: Optional[asyncpg.Pool] = None

# ========= DB 유틸 =========
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS guild_settings(
    guild_id BIGINT PRIMARY KEY,
    nick_channel_id BIGINT,
    create_channel_id BIGINT
);

CREATE TABLE IF NOT EXISTS personal_channels(
    channel_id BIGINT PRIMARY KEY,
    owner_id BIGINT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS blog(
    channel_id BIGINT PRIMARY KEY,
    url TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dashboards(
    channel_id BIGINT PRIMARY KEY,
    message_id BIGINT
);
"""

async def init_db():
    async with PG_POOL.acquire() as con:
        async with con.transaction():
            await con.execute(SCHEMA_SQL)

async def get_settings(guild_id:int):
    async with PG_POOL.acquire() as con:
        row = await con.fetchrow(
            "SELECT nick_channel_id, create_channel_id FROM guild_settings WHERE guild_id=$1",
            guild_id
        )
    return (row["nick_channel_id"], row["create_channel_id"]) if row else (None, None)

async def set_setting(guild_id:int, key:str, value:Optional[int]):
    async with PG_POOL.acquire() as con:
        async with con.transaction():
            await con.execute(
                "INSERT INTO guild_settings(guild_id) VALUES($1) ON CONFLICT (guild_id) DO NOTHING",
                guild_id
            )
            await con.execute(
                f"UPDATE guild_settings SET {key}=$1 WHERE guild_id=$2",
                value, guild_id
            )

async def set_personal_channel(channel_id:int, owner_id:int):
    async with PG_POOL.acquire() as con:
        await con.execute(
            "INSERT INTO personal_channels(channel_id, owner_id) VALUES($1,$2) "
            "ON CONFLICT (channel_id) DO UPDATE SET owner_id=EXCLUDED.owner_id",
            channel_id, owner_id
        )

async def get_owner(channel_id:int) -> Optional[int]:
    async with PG_POOL.acquire() as con:
        row = await con.fetchrow(
            "SELECT owner_id FROM personal_channels WHERE channel_id=$1", channel_id
        )
    return int(row["owner_id"]) if row else None

async def set_blog(channel_id:int, url:Optional[str]):
    async with PG_POOL.acquire() as con:
        if url is None:
            await con.execute("DELETE FROM blog WHERE channel_id=$1", channel_id)
        else:
            await con.execute(
                "INSERT INTO blog(channel_id, url) VALUES($1,$2) "
                "ON CONFLICT (channel_id) DO UPDATE SET url=EXCLUDED.url",
                channel_id, url
            )

async def get_blog(channel_id:int) -> Optional[str]:
    async with PG_POOL.acquire() as con:
        row = await con.fetchrow("SELECT url FROM blog WHERE channel_id=$1", channel_id)
    return str(row["url"]) if row else None

async def set_dashboard_message_id(channel_id:int, message_id:Optional[int]):
    async with PG_POOL.acquire() as con:
        await con.execute(
            "INSERT INTO dashboards(channel_id, message_id) VALUES($1,$2) "
            "ON CONFLICT (channel_id) DO UPDATE SET message_id=EXCLUDED.message_id",
            channel_id, message_id
        )

async def get_dashboard_message_id(channel_id:int) -> Optional[int]:
    async with PG_POOL.acquire() as con:
        row = await con.fetchrow("SELECT message_id FROM dashboards WHERE channel_id=$1", channel_id)
    return int(row["message_id"]) if row and row["message_id"] is not None else None

async def get_channel_by_owner(owner_id:int) -> Optional[int]:
    async with PG_POOL.acquire() as con:
        row = await con.fetchrow(
            "SELECT channel_id FROM personal_channels WHERE owner_id=$1", owner_id
        )
    return int(row["channel_id"]) if row else None

async def purge_channel_records(channel_id:int):
    async with PG_POOL.acquire() as con:
        async with con.transaction():
            await con.execute("DELETE FROM dashboards WHERE channel_id=$1", channel_id)
            await con.execute("DELETE FROM blog WHERE channel_id=$1", channel_id)
            await con.execute("DELETE FROM personal_channels WHERE channel_id=$1", channel_id)

# ========= 유틸 =========
def slugify_channel_name(name:str) -> str:
    s = name.strip().lower()
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"[^a-z0-9\-\_]", "", s)
    return s[:90] if s else "personal"

def sanitize_nick(nick:str) -> str:
    nick = nick.strip()
    nick = nick.replace("@everyone", "everyone").replace("@here", "here")
    return nick[:32] if nick else " "

def is_admin_or_mod(member:discord.Member) -> bool:
    return member.guild_permissions.manage_guild or member.guild_permissions.administrator

async def ensure_dashboard_at_bottom(channel:discord.TextChannel):
    url = await get_blog(channel.id)
    if not url:
        return
    old_id = await get_dashboard_message_id(channel.id)
    if old_id:
        with contextlib.suppress(discord.NotFound, discord.Forbidden, discord.HTTPException):
            msg = await channel.fetch_message(old_id)
            await msg.delete()
    embed = discord.Embed(title="📌 블로그", description=f"[여기를 눌러 열기]({url})", color=0xFF7710)
    embed.set_footer(text="이 채널의 대시보드")
    new_msg = await channel.send(embed=embed)
    await set_dashboard_message_id(channel.id, new_msg.id)

# ========= 봇 이벤트 =========
@BOT.event
async def on_ready():
    global PG_POOL
    if not DATABASE_URL:
        raise RuntimeError("환경변수 DATABASE_URL이 필요합니다. (postgres://...)")

    # 커넥션 풀 만들기
    PG_POOL = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    await init_db()

    try:
        await BOT.tree.sync()
    except Exception as e:
        print("Sync error:", e)

    print(f"Logged in as {BOT.user} (ID: {BOT.user.id})")

@BOT.event
async def on_message(message:discord.Message):
    if message.author.bot or not message.guild:
        return

    nick_ch, create_ch = await get_settings(message.guild.id)

    # 2) 닉변 채널
    if nick_ch and message.channel.id == nick_ch:
        new_nick = sanitize_nick(message.content)
        with contextlib.suppress(discord.Forbidden, discord.HTTPException):
            await message.author.edit(nick=new_nick if new_nick.strip() else None)
        with contextlib.suppress(discord.HTTPException):
            await message.add_reaction("✅")
            await asyncio.sleep(1.0)
            await message.delete()
        return

    # 3~5) 개인채널 생성 채널
    if create_ch and message.channel.id == create_ch:
        existing = await get_channel_by_owner(message.author.id)
        if existing:
            with contextlib.suppress(discord.HTTPException):
                await message.add_reaction("❌")
            ch = message.guild.get_channel(existing)
            if ch:
                await message.reply(f"{message.author.mention} 이미 개인 채널이 있어요: {ch.mention}", mention_author=False)
            else:
                await message.reply(f"{message.author.mention} 이미 개인 채널이 등록되어 있어요. 먼저 `/채널삭제`로 정리해 주세요.", mention_author=False)
            return
        name = slugify_channel_name(message.content or f"{message.author.name}-channel")
        guild = message.guild
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False),
            message.author: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, manage_messages=True),
        }
        new_channel = await guild.create_text_channel(name=name, overwrites=overwrites, reason=f"개인채널 생성 by {message.author}")
        await set_personal_channel(new_channel.id, message.author.id)
        await new_channel.send(
            f"{message.author.mention} 님의 개인 채널이 생성되었습니다.\n"
            f"- 다른 유저: **보기만 가능**\n"
            f"- 채널 이름 변경: 직접 변경 가능(권한 부여됨)\n"
            f"- 블로그 등록: `/블로그등록 url:...` , 삭제: `/블로그삭제`"
        )
        with contextlib.suppress(discord.HTTPException):
            await message.add_reaction("✅")
        return

    # 7) 개인채널이면 대시보드 최신 유지
    owner_id = await get_owner(message.channel.id)
    if owner_id:
        await ensure_dashboard_at_bottom(message.channel)

# ========= 슬래시 커맨드(관리자 전용 그룹) =========
class GuildAdmin(app_commands.Group):
    pass

admin = GuildAdmin(
    name="설정",
    description="관리자 전용 설정",
    guild_only=True
)

@admin.command(name="닉변채널지정", description="닉네임 변경 채널을 지정합니다. (관리자 전용)")
@app_commands.describe(channel="닉변 채널로 사용할 텍스트 채널")
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
async def set_nick_channel(interaction:discord.Interaction, channel:discord.TextChannel):
    await set_setting(interaction.guild.id, "nick_channel_id", channel.id)
    await interaction.response.send_message(f"닉변 채널이 {channel.mention} 로 설정되었습니다.", ephemeral=True)

@admin.command(name="개인채널생성채널지정", description="개인채널 생성 채널을 지정합니다. (관리자 전용)")
@app_commands.describe(channel="개인채널 생성용 텍스트 채널")
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
async def set_create_channel(interaction:discord.Interaction, channel:discord.TextChannel):
    await set_setting(interaction.guild.id, "create_channel_id", channel.id)
    await interaction.response.send_message(f"개인채널 생성 채널이 {channel.mention} 로 설정되었습니다.", ephemeral=True)

BOT.tree.add_command(admin)

# 개인채널 소유자용 커맨드
@BOT.tree.command(name="블로그등록", description="현재 개인 채널에 블로그 URL을 등록합니다.", guild_only=True)
@app_commands.describe(url="블로그 주소 (https://...)")
async def blog_register(interaction:discord.Interaction, url:str):
    if not isinstance(interaction.channel, discord.TextChannel):
        return await interaction.response.send_message("텍스트 채널에서만 사용 가능합니다.", ephemeral=True)

    owner_id = await get_owner(interaction.channel.id)
    if not owner_id:
        return await interaction.response.send_message("여기는 개인 채널이 아닙니다.", ephemeral=True)
    if owner_id != interaction.user.id and not is_admin_or_mod(interaction.user):
        return await interaction.response.send_message("이 개인 채널의 소유자만 등록할 수 있어요.", ephemeral=True)

    if not re.match(r"^https?://", url):
        return await interaction.response.send_message("URL은 http(s)로 시작해야 해요.", ephemeral=True)

    await set_blog(interaction.channel.id, url)
    await interaction.response.send_message("블로그가 등록되었습니다. 대시보드를 갱신할게요.", ephemeral=True)
    await ensure_dashboard_at_bottom(interaction.channel)

@BOT.tree.command(name="블로그삭제", description="현재 개인 채널의 블로그 등록을 해제합니다.", guild_only=True)
async def blog_remove(interaction:discord.Interaction):
    if not isinstance(interaction.channel, discord.TextChannel):
        return await interaction.response.send_message("텍스트 채널에서만 사용 가능합니다.", ephemeral=True)

    owner_id = await get_owner(interaction.channel.id)
    if not owner_id:
        return await interaction.response.send_message("여기는 개인 채널이 아닙니다.", ephemeral=True)
    if owner_id != interaction.user.id and not is_admin_or_mod(interaction.user):
        return await interaction.response.send_message("이 개인 채널의 소유자만 삭제할 수 있어요.", ephemeral=True)

    await set_blog(interaction.channel.id, None)

    old_id = await get_dashboard_message_id(interaction.channel.id)
    if old_id:
        with contextlib.suppress(Exception):
            msg = await interaction.channel.fetch_message(old_id)
            await msg.delete()
        await set_dashboard_message_id(interaction.channel.id, None)

    await interaction.response.send_message("블로그가 삭제되었습니다.", ephemeral=True)

@BOT.tree.command(name="채널삭제", description="현재 개인 채널을 삭제합니다.", guild_only=True)
async def delete_personal_channel(interaction: discord.Interaction):
    # 텍스트 채널만
    if not isinstance(interaction.channel, discord.TextChannel):
        return await interaction.response.send_message("텍스트 채널에서만 사용 가능합니다.", ephemeral=True)

    # 개인채널 여부/소유자 확인
    owner_id = await get_owner(interaction.channel.id)
    if not owner_id:
        return await interaction.response.send_message("여기는 개인 채널이 아닙니다.", ephemeral=True)
    if owner_id != interaction.user.id:
        return await interaction.response.send_message("본인 개인 채널에서만 사용할 수 있어요.", ephemeral=True)

    # 삭제 안내 후 삭제
    await interaction.response.send_message("이 채널을 삭제합니다. 3초 후 삭제돼요.", ephemeral=True)
    await asyncio.sleep(3)

    # DB 정리 → 채널 삭제
    await purge_channel_records(interaction.channel.id)
    with contextlib.suppress(discord.Forbidden, discord.HTTPException):
        await interaction.channel.delete(reason=f"/채널삭제 by {interaction.user}")


# ========= 실행 =========
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("환경변수 DISCORD_TOKEN을 설정하세요.")
    if not DATABASE_URL:
        raise SystemExit("환경변수 DATABASE_URL을 설정하세요. (postgres://...)")
    BOT.run(TOKEN)
