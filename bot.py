# bot.py
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
DATABASE_URL = os.getenv("DATABASE_URL")  # (권장) Pooler URI + ?sslmode=require
TEST_GUILD_ID = int(os.getenv("TEST_GUILD_ID", "0"))  # 테스트 서버 ID(선택). 있으면 길드 싱크로 즉시 반영
PORT = int(os.getenv("PORT", "10000"))               # Web 서비스일 때만 사용
COMMAND_PREFIX = "!"
INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.members = True
BOT = commands.Bot(command_prefix=COMMAND_PREFIX, intents=INTENTS)

# ========= SSL 컨텍스트 =========
def make_ssl_ctx() -> ssl.SSLContext:
    insecure = os.getenv("DB_SSL_INSECURE", "1") == "1"  # 기본 1(테스트). 운영은 0 권장
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

# ========= DB 유틸 =========
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

-- 다중 블로그 가능 + 제목 지원
CREATE TABLE IF NOT EXISTS blog(
    channel_id BIGINT NOT NULL,
    url TEXT NOT NULL,
    title TEXT,
    PRIMARY KEY (channel_id, url)
);

CREATE TABLE IF NOT EXISTS dashboards(
    channel_id BIGINT PRIMARY KEY,
    message_id BIGINT
);
"""

async def init_db():
    """스키마 생성 + 기존 설치 자동 마이그레이션(무중단)"""
    async with PG_POOL.acquire() as con:
        async with con.transaction():
            await con.execute(SCHEMA_SQL)

            # personal_channels 마이그레이션(길드 기준 유니크)
            await con.execute("ALTER TABLE personal_channels ADD COLUMN IF NOT EXISTS guild_id BIGINT;")
            await con.execute("ALTER TABLE personal_channels DROP CONSTRAINT IF EXISTS personal_channels_owner_id_key;")
            await con.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS personal_channels_guild_owner_idx
                ON personal_channels(guild_id, owner_id);
            """)

            # blog 마이그레이션: title 추가 + 채널당 다중 허용을 위해 PK 교체
            await con.execute("ALTER TABLE blog ADD COLUMN IF NOT EXISTS title TEXT;")
            # 과거 단일 PK 이름은 보통 blog_pkey
            await con.execute("ALTER TABLE blog DROP CONSTRAINT IF EXISTS blog_pkey;")
            # 복합 PK 보장
            await con.execute("ALTER TABLE blog ADD PRIMARY KEY (channel_id, url);")

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

async def set_personal_channel(channel_id:int, owner_id:int, guild_id:int):
    async with PG_POOL.acquire() as con:
        await con.execute(
            "INSERT INTO personal_channels(channel_id, owner_id, guild_id) VALUES($1,$2,$3) "
            "ON CONFLICT (channel_id) DO UPDATE SET owner_id=EXCLUDED.owner_id, guild_id=EXCLUDED.guild_id",
            channel_id, owner_id, guild_id
        )

async def get_owner(channel_id:int) -> Optional[int]:
    async with PG_POOL.acquire() as con:
        row = await con.fetchrow(
            "SELECT owner_id FROM personal_channels WHERE channel_id=$1", channel_id
        )
    return int(row["owner_id"]) if row else None

# --- 블로그: 다중 등록 + 제목 ---
async def add_blog(channel_id:int, url:str, title:Optional[str]):
    async with PG_POOL.acquire() as con:
        await con.execute(
            """
            INSERT INTO blog(channel_id, url, title)
            VALUES($1,$2,$3)
            ON CONFLICT (channel_id, url) DO UPDATE SET title=EXCLUDED.title
            """,
            channel_id, url, title
        )

async def remove_blog(channel_id:int, url:str):
    async with PG_POOL.acquire() as con:
        await con.execute("DELETE FROM blog WHERE channel_id=$1 AND url=$2", channel_id, url)

async def clear_blogs(channel_id:int):
    async with PG_POOL.acquire() as con:
        await con.execute("DELETE FROM blog WHERE channel_id=$1", channel_id)

async def list_blogs(channel_id:int) -> list[tuple[str, Optional[str]]]:
    async with PG_POOL.acquire() as con:
        rows = await con.fetch(
            "SELECT url, title FROM blog WHERE channel_id=$1 ORDER BY url",
            channel_id
        )
    return [(r["url"], r["title"]) for r in rows]

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

async def get_channel_by_owner(guild_id:int, owner_id:int) -> Optional[int]:
    async with PG_POOL.acquire() as con:
        row = await con.fetchrow(
            "SELECT channel_id FROM personal_channels WHERE guild_id=$1 AND owner_id=$2",
            guild_id, owner_id
        )
        if row:
            return int(row["channel_id"])

        # 레거시 보정: guild_id NULL
        legacy = await con.fetchrow(
            "SELECT channel_id FROM personal_channels WHERE owner_id=$1 AND guild_id IS NULL",
            owner_id
        )
        if legacy:
            ch_id = int(legacy["channel_id"])
            ch = BOT.get_channel(ch_id)
            if ch and getattr(ch, "guild", None):
                await con.execute(
                    "UPDATE personal_channels SET guild_id=$1 WHERE channel_id=$2",
                    ch.guild.id, ch_id
                )
                if ch.guild.id == guild_id:
                    return ch_id
            else:
                await purge_channel_records(ch_id)
    return None

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

async def ensure_dashboard_at_bottom(channel:discord.TextChannel):
    """개인 채널 대시보드(해당 채널의 블로그 목록)를 맨 아래로 갱신."""
    items = await list_blogs(channel.id)  # [(url, title), ...]
    if not items:
        # 기록만 남아있을 수 있으니 기존 대시보드 메시지 있으면 지움
        old_id = await get_dashboard_message_id(channel.id)
        if old_id:
            with contextlib.suppress(discord.NotFound, discord.Forbidden, discord.HTTPException):
                msg = await channel.fetch_message(old_id)
                await msg.delete()
            await set_dashboard_message_id(channel.id, None)
        return

    lines = [f"🔗 [{(t or '바로가기')}]({u})" for (u, t) in items]
    embed = discord.Embed(title="📌 블로그 대시보드", description="\n".join(lines), color=0xFF7710)
    embed.set_footer(text="이 채널의 대시보드")

    old_id = await get_dashboard_message_id(channel.id)
    if old_id:
        with contextlib.suppress(discord.NotFound, discord.Forbidden, discord.HTTPException):
            msg = await channel.fetch_message(old_id)
            await msg.delete()

    new_msg = await channel.send(embed=embed)
    await set_dashboard_message_id(channel.id, new_msg.id)

# ========= 서버 전체 블로그 대시보드 =========
SERVER_DASHBOARDS: dict[int, tuple[int, Optional[int]]] = {}  # guild_id -> (channel_id, msg_id)

async def refresh_server_dashboard(guild:discord.Guild):
    """지정된 채널에 서버 전체 블로그 목록(모든 개인채널의 블로그)을 갱신."""
    if guild.id not in SERVER_DASHBOARDS:
        return
    channel_id, old_msg_id = SERVER_DASHBOARDS[guild.id]
    channel = guild.get_channel(channel_id)
    if not channel:
        return

    # DB에서 서버 내 모든 블로그 수집 (제목 포함)
    async with PG_POOL.acquire() as con:
        rows = await con.fetch(
            """
            SELECT b.url, COALESCE(b.title, '열기') AS title, p.owner_id
            FROM blog b
            JOIN personal_channels p ON p.channel_id = b.channel_id
            WHERE p.guild_id = $1
            ORDER BY p.owner_id, b.url
            """,
            guild.id,
        )

    desc = "등록된 블로그가 없습니다." if not rows else \
        "\n".join(f"🔗 [{r['title']}]({r['url']}) - <@{r['owner_id']}>" for r in rows)

    embed = discord.Embed(title="📑 서버 블로그 목록", description=desc, color=0x00BFFF)

    if old_msg_id:
        with contextlib.suppress(discord.NotFound, discord.Forbidden, discord.HTTPException):
            msg = await channel.fetch_message(old_msg_id)
            await msg.delete()

    msg = await channel.send(embed=embed)
    SERVER_DASHBOARDS[guild.id] = (channel.id, msg.id)

# ========= 헬스 서버 & DB 재시도 =========
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
    """외부 DB 연결 안정화를 위해 백오프 재시도."""
    global PG_POOL
    if not DATABASE_URL:
        print("DATABASE_URL is empty; DB features disabled")
        return
    delay = 2
    for attempt in range(1, max_attempts + 1):
        try:
            PG_POOL = await asyncpg.create_pool(
                DATABASE_URL,
                min_size=1,
                max_size=5,
                ssl=SSL_CTX,
                command_timeout=60,
                statement_cache_size=0,   # pgbouncer(pooler) 호환
            )
            await init_db()
            print("DB pool ready")
            return
        except Exception as e:
            print(f"DB connect attempt {attempt} failed: {e}")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
    print("DB connect failed; continuing without DB")

# ========= 봇 이벤트 =========
@BOT.event
async def on_ready():
    print(f"Logged in as {BOT.user} (ID: {BOT.user.id})")

    # 슬래시 명령어 동기화
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

    # DB 연결
    await connect_db_with_retry()

@BOT.event
async def on_message(message:discord.Message):
    if message.author.bot or not message.guild:
        return
    if PG_POOL is None:
        return

    nick_ch, create_ch = await get_settings(message.guild.id)

    # 닉변 채널
    if nick_ch and message.channel.id == nick_ch:
        new_nick = sanitize_nick(message.content)
        with contextlib.suppress(discord.Forbidden, discord.HTTPException):
            await message.author.edit(nick=new_nick.strip() or None)
        with contextlib.suppress(discord.HTTPException):
            await message.add_reaction("✅")
            await asyncio.sleep(1.0)
            await message.delete()
        return

    # 개인채널 생성 채널 (같은 카테고리에 생성)
    if create_ch and message.channel.id == create_ch:
        existing = await get_channel_by_owner(message.guild.id, message.author.id)
        if existing:
            with contextlib.suppress(discord.HTTPException):
                await message.add_reaction("❌")
            ch = message.guild.get_channel(existing)
            if ch:
                await message.reply(f"{message.author.mention} 이미 개인 채널이 있어요: {ch.mention}", mention_author=False)
            else:
                await message.reply(f"{message.author.mention} 이미 개인 채널이 등록되어 있어요. 먼저 /채널삭제로 정리해 주세요.", mention_author=False)
            return

        name = slugify_channel_name(message.content or f"{message.author.name}-channel")
        guild = message.guild
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False),
            message.author: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, manage_messages=True),
        }
        parent_category = message.channel.category
        new_channel = await guild.create_text_channel(
            name=name,
            overwrites=overwrites,
            reason=f"개인채널 생성 by {message.author}",
            category=parent_category
        )
        await set_personal_channel(new_channel.id, message.author.id, message.guild.id)
        await new_channel.send(
            f"{message.author.mention} 님의 개인 채널이 생성되었습니다.\n"
            f"- 다른 유저: **보기만 가능**\n"
            f"- 채널 이름 변경: 직접 변경 가능(권한 부여됨)\n"
            f"- 블로그 등록: /블로그등록 url:<주소> title:<표시이름(선택)> , 삭제: /블로그삭제 url:<주소>\n"
            f"- 전체 삭제: /블로그삭제전체"
        )
        with contextlib.suppress(discord.HTTPException):
            await message.add_reaction("✅")
        return

    # 개인채널이면 대시보드 최신 유지
    owner_id = await get_owner(message.channel.id)
    if owner_id:
        await ensure_dashboard_at_bottom(message.channel)

# ========= 명령어 =========
class GuildAdmin(app_commands.Group): pass
admin = GuildAdmin(name="설정", description="관리자 전용 설정")

@admin.command(name="닉변채널지정", description="닉네임 변경 채널을 지정합니다. (관리자 전용)")
@app_commands.guild_only()
@app_commands.describe(channel="닉변 채널")
@app_commands.default_permissions(manage_guild=True)
async def set_nick_channel(interaction, channel:discord.TextChannel):
    await set_setting(interaction.guild.id, "nick_channel_id", channel.id)
    await interaction.response.send_message(f"닉변 채널이 {channel.mention} 로 설정되었습니다.", ephemeral=True)

@admin.command(name="개인채널생성채널지정", description="개인채널 생성 채널을 지정합니다. (관리자 전용)")
@app_commands.guild_only()
@app_commands.describe(channel="개인채널 생성 채널")
@app_commands.default_permissions(manage_guild=True)
async def set_create_channel(interaction, channel:discord.TextChannel):
    await set_setting(interaction.guild.id, "create_channel_id", channel.id)
    await interaction.response.send_message(f"개인채널 생성 채널이 {channel.mention} 로 설정되었습니다.", ephemeral=True)

BOT.tree.add_command(admin)

# 개인채널 소유자용 블로그 명령어
@BOT.tree.command(name="블로그등록", description="현재 개인 채널에 블로그를 추가합니다.")
@app_commands.guild_only()
@app_commands.describe(url="블로그 주소 (https://...)", title="대시보드 표시 이름(선택)")
async def blog_register(interaction: discord.Interaction, url: str, title: Optional[str] = None):
    owner_id = await get_owner(interaction.channel.id)
    if not owner_id or owner_id != interaction.user.id:
        return await interaction.response.send_message("본인 개인 채널에서만 등록할 수 있어요.", ephemeral=True)
    if not re.match(r"^https?://", url):
        return await interaction.response.send_message("URL은 http(s):// 로 시작해야 해요.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    await add_blog(interaction.channel.id, url, title)
    await ensure_dashboard_at_bottom(interaction.channel)
    await refresh_server_dashboard(interaction.guild)
    await interaction.followup.send("블로그가 등록되었습니다 ✅", ephemeral=True)

@BOT.tree.command(name="블로그삭제", description="현재 개인 채널에서 특정 블로그를 삭제합니다.")
@app_commands.guild_only()
@app_commands.describe(url="삭제할 블로그 주소 (https://...)")
async def blog_remove(interaction: discord.Interaction, url: str):
    owner_id = await get_owner(interaction.channel.id)
    if not owner_id or owner_id != interaction.user.id:
        return await interaction.response.send_message("본인 개인 채널에서만 삭제할 수 있어요.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    await remove_blog(interaction.channel.id, url)
    await ensure_dashboard_at_bottom(interaction.channel)
    await refresh_server_dashboard(interaction.guild)
    await interaction.followup.send("블로그가 삭제되었습니다 ✅", ephemeral=True)

@BOT.tree.command(name="블로그삭제전체", description="현재 개인 채널의 모든 블로그를 삭제합니다.")
@app_commands.guild_only()
async def blog_clear(interaction: discord.Interaction):
    owner_id = await get_owner(interaction.channel.id)
    if not owner_id or owner_id != interaction.user.id:
        return await interaction.response.send_message("본인 개인 채널에서만 삭제할 수 있어요.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    await clear_blogs(interaction.channel.id)
    await ensure_dashboard_at_bottom(interaction.channel)
    await refresh_server_dashboard(interaction.guild)
    await interaction.followup.send("모든 블로그가 삭제되었습니다 ✅", ephemeral=True)

@BOT.tree.command(name="블로그목록", description="서버 전체 블로그 목록을 특정 채널에 게시합니다. (관리자 전용)")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(channel="서버 블로그 대시보드를 표시할 채널")
async def blog_list(interaction: discord.Interaction, channel: discord.TextChannel):
    SERVER_DASHBOARDS[interaction.guild.id] = (channel.id, None)
    await refresh_server_dashboard(interaction.guild)
    await interaction.response.send_message(f"{channel.mention} 에 서버 전체 블로그 목록을 게시했습니다.", ephemeral=True)

@BOT.tree.command(name="채널삭제", description="현재 개인 채널을 삭제합니다.")
@app_commands.guild_only()
async def delete_personal_channel(interaction: discord.Interaction):
    owner_id = await get_owner(interaction.channel.id)
    if not owner_id or owner_id != interaction.user.id:
        return await interaction.response.send_message("본인 개인 채널에서만 사용할 수 있어요.", ephemeral=True)
    await interaction.response.send_message("이 채널을 삭제합니다. 3초 후 삭제돼요.", ephemeral=True)
    await asyncio.sleep(3)
    await purge_channel_records(interaction.channel.id)
    with contextlib.suppress(discord.Forbidden, discord.HTTPException):
        await interaction.channel.delete(reason=f"/채널삭제 by {interaction.user}")

@BOT.tree.command(name="채널삭제강제", description="특정 유저의 개인 채널 기록을 DB에서 제거합니다. (관리자 전용)")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(user="개인 채널 소유자")
async def force_delete_channel(interaction: discord.Interaction, user: discord.User):
    ch_id = await get_channel_by_owner(interaction.guild.id, user.id)
    if not ch_id:
        return await interaction.response.send_message(f"{user.mention} 님의 개인 채널 기록이 없습니다.", ephemeral=True)
    await purge_channel_records(ch_id)
    await interaction.response.send_message(f"{user.mention} 님의 개인 채널 기록을 DB에서 제거했습니다.", ephemeral=True)

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
