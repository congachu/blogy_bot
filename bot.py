import os
import re
import ssl
import asyncio
import contextlib
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import asyncpg
from aiohttp import web  # 헬스 서버용

# ========= 설정 =========
TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
TEST_GUILD_ID = int(os.getenv("TEST_GUILD_ID", "0"))
PORT = int(os.getenv("PORT", "10000"))
COMMAND_PREFIX = "!"
INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.members = True
BOT = commands.Bot(command_prefix=COMMAND_PREFIX, intents=INTENTS)

def make_ssl_ctx() -> ssl.SSLContext:
    insecure = os.getenv("DB_SSL_INSECURE", "1") == "1"
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    else:
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx

SSL_CTX = make_ssl_ctx()
PG_POOL: Optional[asyncpg.Pool] = None

# ========= DB 스키마 =========
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS guild_settings(
    guild_id BIGINT PRIMARY KEY,
    nick_channel_id BIGINT,
    create_channel_id BIGINT
);

CREATE TABLE IF NOT EXISTS personal_channels(
    channel_id BIGINT PRIMARY KEY,
    owner_id  BIGINT NOT NULL,
    guild_id  BIGINT,
    UNIQUE (guild_id, owner_id)
);

-- 여러 블로그 지원
CREATE TABLE IF NOT EXISTS blog(
    channel_id BIGINT NOT NULL,
    url TEXT NOT NULL,
    PRIMARY KEY (channel_id, url)
);

CREATE TABLE IF NOT EXISTS dashboards(
    channel_id BIGINT PRIMARY KEY,
    message_id BIGINT
);
"""

# ========= DB 유틸 =========
async def init_db():
    async with PG_POOL.acquire() as con:
        async with con.transaction():
            await con.execute(SCHEMA_SQL)
            await con.execute("ALTER TABLE personal_channels ADD COLUMN IF NOT EXISTS guild_id BIGINT;")
            await con.execute("ALTER TABLE personal_channels DROP CONSTRAINT IF EXISTS personal_channels_owner_id_key;")
            await con.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS personal_channels_guild_owner_idx
                ON personal_channels(guild_id, owner_id);
            """)

async def set_personal_channel(channel_id:int, owner_id:int, guild_id:int):
    async with PG_POOL.acquire() as con:
        await con.execute(
            "INSERT INTO personal_channels(channel_id, owner_id, guild_id) VALUES($1,$2,$3) "
            "ON CONFLICT (channel_id) DO UPDATE SET owner_id=EXCLUDED.owner_id, guild_id=EXCLUDED.guild_id",
            channel_id, owner_id, guild_id
        )

async def get_owner(channel_id:int) -> Optional[int]:
    async with PG_POOL.acquire() as con:
        row = await con.fetchrow("SELECT owner_id FROM personal_channels WHERE channel_id=$1", channel_id)
    return int(row["owner_id"]) if row else None

async def get_channel_by_owner(guild_id:int, owner_id:int) -> Optional[int]:
    async with PG_POOL.acquire() as con:
        row = await con.fetchrow("SELECT channel_id FROM personal_channels WHERE guild_id=$1 AND owner_id=$2", guild_id, owner_id)
        return int(row["channel_id"]) if row else None

# --- 블로그 관련 ---
async def add_blog(channel_id:int, url:str):
    async with PG_POOL.acquire() as con:
        await con.execute(
            "INSERT INTO blog(channel_id, url) VALUES($1,$2) ON CONFLICT DO NOTHING",
            channel_id, url
        )

async def remove_blog(channel_id:int, url:str):
    async with PG_POOL.acquire() as con:
        await con.execute("DELETE FROM blog WHERE channel_id=$1 AND url=$2", channel_id, url)

async def clear_blogs(channel_id:int):
    async with PG_POOL.acquire() as con:
        await con.execute("DELETE FROM blog WHERE channel_id=$1", channel_id)

async def list_blogs(channel_id:int) -> list[str]:
    async with PG_POOL.acquire() as con:
        rows = await con.fetch("SELECT url FROM blog WHERE channel_id=$1", channel_id)
    return [r["url"] for r in rows]

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
    s = re.sub(r"[^a-z0-9ㄱ-ㅎ가-힣\-_]", "", s)
    s = re.sub(r"-{2,}", "-", s)
    return s[:90] if s else "personal"

def sanitize_nick(nick:str) -> str:
    nick = nick.strip()
    nick = nick.replace("@everyone", "everyone").replace("@here", "here")
    return nick[:32] if nick else " "

def is_admin_or_mod(member:discord.Member) -> bool:
    return member.guild_permissions.manage_guild or member.guild_permissions.administrator

# ========= 대시보드 =========
async def ensure_dashboard_at_bottom(channel:discord.TextChannel):
    urls = await list_blogs(channel.id)
    if not urls:
        return
    lines = [f"🔗 [블로그 열기]({u})" for u in urls]
    embed = discord.Embed(title="📌 블로그 대시보드", description="\n".join(lines), color=0xFF7710)
    embed.set_footer(text="이 채널의 대시보드")

    old_id = await get_dashboard_message_id(channel.id)
    if old_id:
        with contextlib.suppress(discord.NotFound, discord.Forbidden, discord.HTTPException):
            msg = await channel.fetch_message(old_id)
            await msg.delete()

    new_msg = await channel.send(embed=embed)
    await set_dashboard_message_id(channel.id, new_msg.id)

SERVER_DASHBOARDS = {}  # guild_id -> (channel_id, msg_id)

async def refresh_server_dashboard(guild:discord.Guild):
    if guild.id not in SERVER_DASHBOARDS:
        return
    channel_id, old_msg_id = SERVER_DASHBOARDS[guild.id]
    channel = guild.get_channel(channel_id)
    if not channel:
        return

    async with PG_POOL.acquire() as con:
        rows = await con.fetch("""
            SELECT b.url, p.owner_id
            FROM blog b
            JOIN personal_channels p ON b.channel_id = p.channel_id
            WHERE p.guild_id=$1
        """, guild.id)

    if not rows:
        desc = "등록된 블로그가 없습니다."
    else:
        desc = "\n".join(f"🔗 [열기]({r['url']}) - <@{r['owner_id']}>" for r in rows)

    embed = discord.Embed(title="📑 서버 블로그 목록", description=desc, color=0x00BFFF)

    if old_msg_id:
        with contextlib.suppress(discord.NotFound, discord.Forbidden, discord.HTTPException):
            msg = await channel.fetch_message(old_msg_id)
            await msg.delete()

    msg = await channel.send(embed=embed)
    SERVER_DASHBOARDS[guild.id] = (channel.id, msg.id)

# ========= 헬스 서버 =========
async def run_health_server():
    async def health(_):
        return web.Response(text="ok")
    app = web.Application()
    app.router.add_get("/healthz", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    print(f"health server on :{PORT}/healthz")

async def connect_db_with_retry(max_attempts=8):
    global PG_POOL
    if not DATABASE_URL:
        print("DATABASE_URL is empty; DB features disabled")
        return
    delay = 2
    for attempt in range(1, max_attempts+1):
        try:
            PG_POOL = await asyncpg.create_pool(
                DATABASE_URL,
                min_size=1, max_size=5,
                ssl=SSL_CTX, command_timeout=60,
                statement_cache_size=0
            )
            await init_db()
            print("DB pool ready")
            return
        except Exception as e:
            print(f"DB connect attempt {attempt} failed: {e}")
            await asyncio.sleep(delay)
            delay = min(delay*2, 30)
    print("DB connect failed; continuing without DB")

# ========= 이벤트 =========
@BOT.event
async def on_ready():
    print(f"Logged in as {BOT.user} (ID: {BOT.user.id})")
    try:
        if TEST_GUILD_ID:
            guild = discord.Object(id=TEST_GUILD_ID)
            BOT.tree.copy_global_to(guild=guild)
            synced = await BOT.tree.sync(guild=guild)
            print(f"Slash synced to guild {TEST_GUILD_ID}: {len(synced)} cmds")
        else:
            synced = await BOT.tree.sync()
            print(f"Slash synced globally: {len(synced)} cmds")
    except Exception as e:
        print("Sync error:", e)

    await connect_db_with_retry()

@BOT.event
async def on_message(message:discord.Message):
    if message.author.bot or not message.guild:
        return
    if PG_POOL is None:
        return

    # 개인채널이면 대시보드 최신 유지
    owner_id = await get_owner(message.channel.id)
    if owner_id:
        await ensure_dashboard_at_bottom(message.channel)

# ========= 커맨드 =========
@BOT.tree.command(name="블로그등록", description="현재 개인 채널에 블로그를 추가합니다.")
@app_commands.guild_only()
@app_commands.describe(url="블로그 주소 (https://...)")
async def blog_register(interaction:discord.Interaction, url:str):
    owner_id = await get_owner(interaction.channel.id)
    if not owner_id:
        return await interaction.response.send_message("여기는 개인 채널이 아닙니다.", ephemeral=True)
    if owner_id != interaction.user.id:
        return await interaction.response.send_message("본인 개인 채널만 등록할 수 있어요.", ephemeral=True)

    if not re.match(r"^https?://", url):
        return await interaction.response.send_message("URL은 http(s)로 시작해야 해요.", ephemeral=True)

    await add_blog(interaction.channel.id, url)
    await interaction.response.send_message("블로그가 추가되었습니다.", ephemeral=True)
    await ensure_dashboard_at_bottom(interaction.channel)
    await refresh_server_dashboard(interaction.guild)

@BOT.tree.command(name="블로그삭제", description="현재 개인 채널에서 특정 블로그를 삭제합니다.")
@app_commands.guild_only()
@app_commands.describe(url="삭제할 블로그 주소")
async def blog_remove(interaction:discord.Interaction, url:str):
    owner_id = await get_owner(interaction.channel.id)
    if not owner_id:
        return await interaction.response.send_message("여기는 개인 채널이 아닙니다.", ephemeral=True)
    if owner_id != interaction.user.id:
        return await interaction.response.send_message("본인 개인 채널만 삭제할 수 있어요.", ephemeral=True)

    await remove_blog(interaction.channel.id, url)
    await interaction.response.send_message("블로그가 삭제되었습니다.", ephemeral=True)
    await ensure_dashboard_at_bottom(interaction.channel)
    await refresh_server_dashboard(interaction.guild)

@BOT.tree.command(name="블로그삭제전체", description="현재 개인 채널의 모든 블로그를 삭제합니다.")
@app_commands.guild_only()
async def blog_clear(interaction:discord.Interaction):
    owner_id = await get_owner(interaction.channel.id)
    if not owner_id:
        return await interaction.response.send_message("여기는 개인 채널이 아닙니다.", ephemeral=True)
    if owner_id != interaction.user.id:
        return await interaction.response.send_message("본인 개인 채널만 삭제할 수 있어요.", ephemeral=True)

    await clear_blogs(interaction.channel.id)
    await interaction.response.send_message("모든 블로그가 삭제되었습니다.", ephemeral=True)
    await ensure_dashboard_at_bottom(interaction.channel)
    await refresh_server_dashboard(interaction.guild)

@BOT.tree.command(name="블로그목록", description="서버 전체 블로그 목록을 특정 채널에 게시합니다. (관리자 전용)")
@app_commands.guild_only()
@app_commands.describe(channel="목록을 게시할 채널")
@app_commands.checks.has_permissions(manage_guild=True)
async def blog_list(interaction:discord.Interaction, channel:discord.TextChannel):
    SERVER_DASHBOARDS[interaction.guild.id] = (channel.id, None)
    await refresh_server_dashboard(interaction.guild)
    await interaction.response.send_message(f"{channel.mention} 에 블로그 목록을 게시했습니다.", ephemeral=True)

# ========= 실행 =========
async def main():
    if not TOKEN:
        raise SystemExit("환경변수 DISCORD_TOKEN을 설정하세요.")
    await asyncio.gather(
        run_health_server(),
        BOT.start(TOKEN),
    )

if __name__ == "__main__":
    asyncio.run(main())
