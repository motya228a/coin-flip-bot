"""coinflip — Telegram-бот с играми, экономикой и админкой.
Один файл. SQLite WAL, атомарные деньги, HTML-экранирование.
Оплата вручную на @immallier (карта, крипта, NFT, Stars).
Секреты читаются из .env — на GitHub они не попадают.
"""

import asyncio, json, logging, os, re, secrets, string, sys, time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field, fields
from html import escape as h
from typing import Any, AsyncIterator, Optional

import aiosqlite
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup,
    InlineKeyboardButton, BotCommand, ErrorEvent)

# ============================== .ENV ==============================
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    print("⚠️ python-dotenv не установлен. Установи: pip install python-dotenv", file=sys.stderr)

# ============================== CONFIG ==============================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = frozenset(
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
)
NFT_WALLET = os.getenv("NFT_WALLET", "").strip()
DB_PATH = os.getenv("DB_PATH", "data.db")

if not BOT_TOKEN:
    print("❌ BOT_TOKEN не найден. Создай файл .env рядом с bot.py и укажи BOT_TOKEN.", file=sys.stderr)
    sys.exit(1)
if not ADMIN_IDS:
    print("❌ ADMIN_IDS пусто. Укажи в .env свой Telegram ID.", file=sys.stderr)
    sys.exit(1)

COIN_RATE, START_BONUS = 0.20, 100
MIN_BET, MAX_BET = 1, 10_000
MIN_WITHDRAW_CF, MAX_WITHDRAW_CF = 500, 100_000
TOPLIST_SIZE, TOPLIST_MIN_BALANCE = 10, 10
DEPOSIT_CONTACT, MIN_DEPOSIT_RUB = "@immallier", 50

SUBSCRIBE_REWARDS = {
    "CoinFlipChatik": {"chat_id": "@CoinFlipChatik", "reward": 100},
    "KwadrovShop": {"chat_id": "@KwadrovShop", "reward": 100},
}
SUBSCRIBE_TOTAL = sum(r["reward"] for r in SUBSCRIBE_REWARDS.values())

TREASURY_COOLDOWN = 90 * 60
TREASURY_MIN, TREASURY_MAX = 100, 1000
TREASURY_ITEMS = [(100,30),(200,20),(300,15),(400,10),(500,10),(600,5),(700,4),(800,3),(900,2),(1000,1)]
TREASURY_A = [a for a,_ in TREASURY_ITEMS]
TREASURY_W = [w for _,w in TREASURY_ITEMS]
TREASURY_MSG_LIMIT = 120

DAILY_COOLDOWN, DAILY_REWARDS, DAILY_STREAK_BONUS = 24*3600, [100,150,200,250,300,400,500], 50
WEEKLY_COOLDOWN, WEEKLY_REWARD = 7*24*3600, 2000
HOURLY_COOLDOWN, HOURLY_REWARD = 3600, 25
REFERRAL_BONUS_REFERRER, REFERRAL_BONUS_NEW = 100, 50

VIP_PRICE_CF, VIP_PRICE_STARS, VIP_DURATION, VIP_CASHBACK = 5000, 200, 7*24*3600, 0.05
CLAN_CREATE_PRICE, CLAN_MAX_MEMBERS, CLAN_CASHBACK = 10_000, 10, 0.02

QUEST_RESET_COOLDOWN = 24 * 3600
QUEST_POOL = [
    {"key":"play_games","desc":"Сыграй 10 игр","target":10,"reward":200},
    {"key":"win_games","desc":"Выиграй 5 игр","target":5,"reward":300},
    {"key":"bet_1000","desc":"Ставок на 1000 CF","target":1000,"reward":250},
    {"key":"treasury_3","desc":"Забери казну 3 раза","target":3,"reward":250},
    {"key":"mines_open","desc":"Открой 20 клеток в Mines","target":20,"reward":200},
    {"key":"win_streak","desc":"3 победы подряд","target":3,"reward":400},
]

LEVELS = [(0,"🥉 Новичок"),(500,"🥈 Игрок"),(2000,"🥇 Про"),(5000,"💎 Мастер"),
          (10000,"👑 Легенда"),(25000,"🔥 Бог игры"),(100000,"🌟 Титан")]

ACHIEVEMENTS = {
    "first_game":{"name":"Первый шаг","reward":50},
    "first_win":{"name":"Первая победа","reward":50},
    "ten_games":{"name":"Десятка","reward":100},
    "hundred_games":{"name":"Сотка","reward":500},
    "thousand_games":{"name":"Тысячник","reward":5000},
    "big_win":{"name":"Крупный куш","reward":200},
    "huge_win":{"name":"Джекпот","reward":2000},
    "first_deposit":{"name":"Донатер","reward":100},
    "subscribed":{"name":"Подписчик","reward":150},
    "treasury_hunter":{"name":"Охотник за казной","reward":100},
    "millionaire":{"name":"Миллионер","reward":5000},
    "vip":{"name":"VIP","reward":500},
    "clan_owner":{"name":"Лидер","reward":1000},
    "big_wager":{"name":"Хайроллер","reward":1000},
    "streak_10":{"name":"Огонь","reward":1500},
}

MINES_SIZE, MINES_COUNT = 5, 3
SHOP_ITEMS = {
    "bonus_1000":{"name":"Бонус 1000 CF","price":1500,"desc":"+1000 CF (бонусные)"},
    "bonus_5000":{"name":"Бонус 5000 CF","price":6000,"desc":"+5000 CF (бонусные)"},
    "streak_shield":{"name":"Щит стрика","price":3000,"desc":"Сохраняет daily-стрик"},
    "luck_charm":{"name":"Талисман","price":8000,"desc":"-5% к проигрышам 24ч"},
    "xp_boost":{"name":"Буст уровня","price":5000,"desc":"+1000 к обороту"},
}
TOURNAMENT_DURATION = 24*3600
TOURNAMENT_REWARD_TOP1, TOURNAMENT_REWARD_TOP2, TOURNAMENT_REWARD_TOP3 = 5000, 2000, 1000

TON_RE = re.compile(r"^(?:EQ|UQ|kQ|0Q)[A-Za-z0-9_-]{46}$")
MIR_RE = re.compile(r"^2\d{15}$")
NAME_RE = re.compile(r"^[A-Za-zА-Яа-я0-9 _-]{2,20}$")
KILO_RE = re.compile(r"^(\d+)\s*[кkКK]$")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("coinflip")
RNG = secrets.SystemRandom()

# ============================== LOCKS ==============================
_locks: dict[str, asyncio.Lock] = {}
def lock(key: str) -> asyncio.Lock:
    lk = _locks.get(key)
    if lk is None:
        lk = asyncio.Lock()
        _locks[key] = lk
    return lk
def ulock(uid: int) -> asyncio.Lock: return lock(f"u:{uid}")
def glock(key: str) -> asyncio.Lock: return lock(f"g:{key}")

# ============================== MODEL ==============================
@dataclass
class User:
    user_id: int
    username: str = ""
    balance: int = 0
    bonus_balance: int = 0
    total_wins: int = 0
    total_games: int = 0
    total_wagered: int = 0
    win_streak: int = 0
    best_win_streak: int = 0
    registered_at: float = 0.0
    total_deposits_cf: int = 0
    total_withdraws_cf: int = 0
    last_treasury_at: float = 0.0
    last_daily_at: float = 0.0
    last_weekly_at: float = 0.0
    last_hourly_at: float = 0.0
    daily_streak: int = 0
    treasury_count: int = 0
    mines_opened_total: int = 0
    referrer_id: Optional[int] = None
    referral_count: int = 0
    last_quest_reset: float = 0.0
    vip_until: float = 0.0
    clan_id: Optional[int] = None
    banned: bool = False
    luck_charm_until: float = 0.0
    streak_shield: int = 0
    tournament_score: int = 0
    subscribed_channels: list = field(default_factory=list)
    achievements: list = field(default_factory=list)
    promos_used: list = field(default_factory=list)
    transactions: list = field(default_factory=list)
    quests: list = field(default_factory=list)

    @property
    def paid_balance(self) -> int: return max(0, self.balance - self.bonus_balance)
    @property
    def is_vip(self) -> bool: return self.vip_until > time.time()
    @property
    def has_luck_charm(self) -> bool: return self.luck_charm_until > time.time()
    def wait_treasury(self) -> int: return max(0, int(self.last_treasury_at + TREASURY_COOLDOWN - time.time()))
    def wait_daily(self) -> int: return max(0, int(self.last_daily_at + DAILY_COOLDOWN - time.time()))
    def wait_weekly(self) -> int: return max(0, int(self.last_weekly_at + WEEKLY_COOLDOWN - time.time()))
    def wait_hourly(self) -> int: return max(0, int(self.last_hourly_at + HOURLY_COOLDOWN - time.time()))
    def level(self) -> str:
        n = LEVELS[0][1]
        for thr, nm in LEVELS:
            if self.total_wagered >= thr: n = nm
        return n
    def next_level(self) -> tuple[int,int,str]:
        for thr, nm in LEVELS:
            if self.total_wagered < thr: return self.total_wagered, thr, nm
        return self.total_wagered, self.total_wagered, "МАКС"
    def cashback_rate(self) -> float:
        r = 0.0
        if self.is_vip: r += VIP_CASHBACK
        if self.clan_id is not None: r += CLAN_CASHBACK
        return r
    def add_tx(self, kind: str, amount: int, note: str = "") -> None:
        self.transactions.append({"ts": time.time(), "kind": kind,
                                   "amount": int(amount), "note": str(note)[:200]})
        if len(self.transactions) > 100: self.transactions = self.transactions[-100:]
    @classmethod
    def from_row(cls, row) -> "User":
        d = {k: row[k] for k in row.keys() if k != "extra"}
        ex: dict = {}
        raw = row["extra"] if "extra" in row.keys() else None
        if raw:
            try:
                p = json.loads(raw)
                if isinstance(p, dict): ex = p
            except (json.JSONDecodeError, TypeError): pass
        for k in ("subscribed_channels","achievements","promos_used","transactions","quests"):
            d[k] = ex.get(k, []) or []
        if "banned" in d: d["banned"] = bool(d["banned"])
        names = {f.name for f in fields(cls)}
        clean = {k: v for k, v in d.items() if k in names}
        return cls(**clean)

# ============================== DB ==============================
DB: Optional[aiosqlite.Connection] = None
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY, username TEXT DEFAULT '',
    balance INTEGER DEFAULT 0, bonus_balance INTEGER DEFAULT 0,
    total_wins INTEGER DEFAULT 0, total_games INTEGER DEFAULT 0,
    total_wagered INTEGER DEFAULT 0, win_streak INTEGER DEFAULT 0,
    best_win_streak INTEGER DEFAULT 0, registered_at REAL DEFAULT 0,
    total_deposits_cf INTEGER DEFAULT 0, total_withdraws_cf INTEGER DEFAULT 0,
    last_treasury_at REAL DEFAULT 0, last_daily_at REAL DEFAULT 0,
    last_weekly_at REAL DEFAULT 0, last_hourly_at REAL DEFAULT 0,
    daily_streak INTEGER DEFAULT 0, treasury_count INTEGER DEFAULT 0,
    mines_opened_total INTEGER DEFAULT 0, referrer_id INTEGER DEFAULT NULL,
    referral_count INTEGER DEFAULT 0, last_quest_reset REAL DEFAULT 0,
    vip_until REAL DEFAULT 0, clan_id INTEGER DEFAULT NULL,
    banned INTEGER DEFAULT 0, luck_charm_until REAL DEFAULT 0,
    streak_shield INTEGER DEFAULT 0, tournament_score INTEGER DEFAULT 0,
    extra TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_u_balance ON users(balance DESC);
CREATE INDEX IF NOT EXISTS idx_u_wins ON users(total_wins DESC);
CREATE INDEX IF NOT EXISTS idx_u_streak ON users(best_win_streak DESC);
CREATE INDEX IF NOT EXISTS idx_u_tour ON users(tournament_score DESC);
CREATE INDEX IF NOT EXISTS idx_u_uname ON users(username);

CREATE TABLE IF NOT EXISTS withdraws (
    request_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL, username TEXT DEFAULT '',
    amount_cf INTEGER NOT NULL, amount_rub REAL NOT NULL,
    method TEXT NOT NULL, destination TEXT NOT NULL,
    created_at REAL NOT NULL, status TEXT DEFAULT 'pending',
    tx_hash TEXT DEFAULT '', processed_by INTEGER DEFAULT NULL,
    processed_at REAL DEFAULT NULL
);
CREATE INDEX IF NOT EXISTS idx_w_status ON withdraws(status);

CREATE TABLE IF NOT EXISTS clans (
    clan_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE, owner_id INTEGER NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS clan_members (
    clan_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
    joined_at REAL NOT NULL, PRIMARY KEY (clan_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_cm_user ON clan_members(user_id);

CREATE TABLE IF NOT EXISTS duels (
    duel_id INTEGER PRIMARY KEY AUTOINCREMENT,
    creator_id INTEGER NOT NULL, opponent_id INTEGER DEFAULT NULL,
    amount INTEGER NOT NULL, status TEXT DEFAULT 'open',
    created_at REAL NOT NULL, finished_at REAL DEFAULT NULL,
    winner_id INTEGER DEFAULT NULL
);
CREATE INDEX IF NOT EXISTS idx_d_status ON duels(status);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL, kind TEXT NOT NULL,
    amount INTEGER NOT NULL, note TEXT DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tx_user ON transactions(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS nft_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL, method TEXT NOT NULL,
    address_or_link TEXT NOT NULL, note TEXT DEFAULT '',
    status TEXT DEFAULT 'pending', created_at REAL NOT NULL,
    processed_at REAL DEFAULT NULL
);
"""

async def db_init() -> None:
    global DB
    DB = await aiosqlite.connect(DB_PATH, timeout=30)
    DB.row_factory = aiosqlite.Row
    for pragma in ("busy_timeout=5000","journal_mode=WAL","synchronous=NORMAL","foreign_keys=ON"):
        await DB.execute(f"PRAGMA {pragma}")
    await DB.executescript(SCHEMA)
    await DB.commit()
    for name in ("withdraw","clan","duel"):
        tbl = "withdraws" if name == "withdraw" else ("clans" if name == "clan" else "duels")
        col = "request_id" if name == "withdraw" else ("clan_id" if name == "clan" else "duel_id")
        async with DB.execute(f"SELECT COALESCE(MAX({col}),0) AS m FROM {tbl}") as cur:
            row = await cur.fetchone()
        await DB.execute(
            "INSERT INTO counters(name,value) VALUES(?,?) "
            "ON CONFLICT(name) DO UPDATE SET value=MAX(value,excluded.value)",
            (name, row["m"]))
    await DB.commit()

@asynccontextmanager
async def tx(immediate: bool = True) -> AsyncIterator[None]:
    await DB.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
        await DB.commit()
    except Exception:
        await DB.rollback()
        raise

async def kv_get(key: str, default: Any = None) -> Any:
    async with DB.execute("SELECT value FROM kv WHERE key=?", (key,)) as cur:
        row = await cur.fetchone()
    if row is None: return default
    try:
        v = json.loads(row["value"]); return v
    except (json.JSONDecodeError, TypeError): return default

async def kv_set(key: str, value: Any) -> None:
    await DB.execute(
        "INSERT INTO kv(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value, ensure_ascii=False)))
    await DB.commit()

async def next_id(name: str) -> int:
    async with glock(f"counter:{name}"):
        await DB.execute(
            "INSERT INTO counters(name,value) VALUES(?,1) "
            "ON CONFLICT(name) DO UPDATE SET value=value+1",
            (name,))
        await DB.commit()
        async with DB.execute("SELECT value FROM counters WHERE name=?", (name,)) as cur:
            row = await cur.fetchone()
    return int(row["value"])

async def _read(uid: int) -> Optional[User]:
    async with DB.execute("SELECT * FROM users WHERE user_id=?", (uid,)) as cur:
        row = await cur.fetchone()
    return User.from_row(row) if row else None

async def _get_locked(uid: int, username: Optional[str] = None) -> User:
    u = await _read(uid)
    if u is None:
        await DB.execute(
            "INSERT OR IGNORE INTO users(user_id,username,balance,registered_at) "
            "VALUES(?,?,?,?)",
            (uid, username or "", START_BONUS, time.time()))
        await DB.commit()
        u = await _read(uid)
        if u is None: raise RuntimeError(f"cannot create user {uid}")
    if username and u.username != username:
        await DB.execute("UPDATE users SET username=? WHERE user_id=?", (username, uid))
        await DB.commit()
        u.username = username
    return u

async def get_user(uid: int, username: Optional[str] = None) -> User:
    async with ulock(uid):
        return await _get_locked(uid, username)

async def find_user_by_username(username: str) -> Optional[User]:
    uname = username.lstrip("@")
    async with DB.execute("SELECT * FROM users WHERE LOWER(username)=LOWER(?)", (uname,)) as cur:
        row = await cur.fetchone()
    return User.from_row(row) if row else None

async def top_users(field_key: str, limit: int = 10, min_val: int = 0) -> list[User]:
    F = {"balance":"balance","wins":"total_wins","streak":"best_win_streak","tournament":"tournament_score"}
    f = F.get(field_key, "balance")
    min_val = max(0, int(min_val))
    async with DB.execute(
        f"SELECT * FROM users WHERE {f} >= ? ORDER BY {f} DESC LIMIT ?",
        (min_val, limit)) as cur:
        rows = await cur.fetchall()
    return [User.from_row(r) for r in rows]

async def _update_balance(uid: int, delta: int, bonus_delta: int = 0) -> None:
    await DB.execute(
        "UPDATE users SET balance=balance+?, bonus_balance=bonus_balance+? WHERE user_id=?",
        (delta, bonus_delta, uid))

async def _touch_tx(uid: int, kind: str, amount: int, note: str = "") -> None:
    await DB.execute(
        "INSERT INTO transactions(user_id,kind,amount,note,created_at) VALUES(?,?,?,?,?)",
        (uid, kind, amount, note[:200], time.time()))

async def apply_loss(uid: int, bet: int, kind: str, note: str = "") -> int:
    async with ulock(uid):
        async with tx():
            u = await _read(uid)
            if u is None or u.balance < bet: return -1
            bp = min(u.bonus_balance, bet)
            await DB.execute(
                "UPDATE users SET balance=balance-?, bonus_balance=bonus_balance-?, "
                "win_streak=0, total_games=total_games+1, total_wagered=total_wagered+? "
                "WHERE user_id=?",
                (bet, bp, bet, uid))
            await _touch_tx(uid, kind, -bet, note)
            cb = int(bet * u.cashback_rate())
            if cb > 0:
                await _update_balance(uid, cb, 0)
                await _touch_tx(uid, "cashback", cb, "Кэшбэк")
            return cb

async def persist_extra(u: User) -> None:
    ex = {"subscribed_channels": u.subscribed_channels,
          "achievements": u.achievements, "promos_used": u.promos_used,
          "transactions": u.transactions, "quests": u.quests}
    await DB.execute("UPDATE users SET extra=? WHERE user_id=?",
                     (json.dumps(ex, ensure_ascii=False), u.user_id))
    await DB.commit()

# ============================== UTILS ==============================
def fmt_rub(cf: int) -> str: return f"{cf * COIN_RATE:.2f} ₽"
def fmt_age(s: float) -> str:
    if s < 60: return "меньше минуты"
    if s < 3600: return f"{int(s//60)} мин"
    if s < 86400: return f"{int(s//3600)} ч"
    return f"{int(s//86400)} д"
def fmt_cd(s: int) -> str:
    if s <= 0: return "готово"
    if s < 60: return f"{s} сек"
    if s < 3600: return f"{s//60} мин {s%60} сек"
    if s < 86400: return f"{s//3600} ч {(s%3600)//60} мин"
    return f"{s//86400} д {(s%86400)//3600} ч"
def fmt_big(n) -> str:
    try: n = int(n)
    except (TypeError, ValueError): n = 0
    if n >= 1_000_000: return f"{n/1_000_000:.2f}M"
    if n >= 1_000: return f"{n/1_000:.1f}K"
    return str(n)
def esc(t: Any) -> str: return h(str(t or ""), quote=False)
def udisp(u: User) -> str: return f"@{esc(u.username)}" if u.username else f"ID{u.user_id}"
def umention(uid: int, username: str = "") -> str:
    return f"@{esc(username)}" if username else f'<a href="tg://user?id={uid}">ID{uid}</a>'
def is_ton(a: str) -> bool: return bool(TON_RE.match(a.strip()))
def is_mir(c: str) -> bool: return bool(MIR_RE.match(c.strip().replace(" ", "")))
def is_admin(uid: Optional[int]) -> bool: return uid is not None and uid in ADMIN_IDS

def parse_bet(text: Optional[str], u: User) -> tuple[Optional[int], Optional[str]]:
    if not text: return None, "❌ Введи число."
    s = re.sub(r"^@\w+\s*", "", text.strip())
    lo = s.lower()
    if lo in ("all","все","вабанк","всё","олл"):
        if u.balance < MIN_BET: return None, "❌ Мало средств."
        return min(u.balance, MAX_BET), None
    if lo in ("пол","половина","half"):
        v = u.balance // 2
        if v < MIN_BET: return None, "❌ Мало."
        return min(v, MAX_BET), None
    m = KILO_RE.match(s)
    if m: s = str(int(m.group(1)) * 1000)
    s = s.replace(" ", "").replace("_", "")
    try: bet = int(s)
    except ValueError: return None, "❌ Целое число."
    if bet < MIN_BET or bet > MAX_BET: return None, f"❌ Ставка {MIN_BET}–{MAX_BET}."
    if bet > u.balance: return None, f"❌ Баланс {u.balance}."
    return bet, None

def roll_treasury() -> int: return RNG.choices(TREASURY_A, weights=TREASURY_W, k=1)[0]

def _norm(t: Optional[str]) -> str:
    if not t: return ""
    c = re.sub(r"[\u200b-\u200f\ufeff\u00a0]", "", t)
    c = re.sub(r"^@\w+\s*", "", c)
    c = re.sub(r"[^\w]+", "", c, flags=re.UNICODE).lower()
    return c

def is_treasury_cmd(t: Optional[str]) -> bool:
    return _norm(t) in ("казна", "казну", "казны", "казнау", "казнаа")
def is_daily_cmd(t: Optional[str]) -> bool:
    return _norm(t) in ("бонус", "бонусы", "дейли", "ежедневка", "daily")
def is_hourly_cmd(t: Optional[str]) -> bool:
    return _norm(t) in ("час", "часовой", "hourly")
def is_weekly_cmd(t: Optional[str]) -> bool:
    return _norm(t) in ("неделя", "недельный", "weekly")

def gen_promo(n: int = 10) -> str:
    return "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(n))

async def is_subscribed(b: Bot, uid: int, ch: str) -> bool:
    try:
        m = await b.get_chat_member(chat_id=ch, user_id=uid)
        return m.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
    except TelegramAPIError as e:
        log.warning("sub %s: %s", ch, e)
        return False

def check_ach(u: User) -> list[str]:
    new = []
    conds = {
        "first_game": u.total_games >= 1, "first_win": u.total_wins >= 1,
        "ten_games": u.total_games >= 10, "hundred_games": u.total_games >= 100,
        "thousand_games": u.total_games >= 1000,
        "big_win": any(t.get("kind")=="game_win" and t.get("amount",0)>=1000 for t in u.transactions),
        "huge_win": any(t.get("kind")=="game_win" and t.get("amount",0)>=10000 for t in u.transactions),
        "first_deposit": u.total_deposits_cf > 0,
        "subscribed": len(u.subscribed_channels) >= len(SUBSCRIBE_REWARDS),
        "treasury_hunter": u.treasury_count > 0,
        "millionaire": u.balance >= 1_000_000,
        "vip": u.is_vip, "clan_owner": u.clan_id is not None,
        "big_wager": u.total_wagered >= 10_000,
        "streak_10": u.best_win_streak >= 10,
    }
    for k, ok in conds.items():
        if ok and k not in u.achievements:
            a = ACHIEVEMENTS[k]
            u.achievements.append(k)
            new.append(f"{a['name']} (+{a['reward']} CF)")
    return new

def roll_quests(u: User) -> None:
    n = min(3, len(QUEST_POOL))
    chosen = RNG.sample(QUEST_POOL, k=n)
    u.quests = []
    for q in chosen:
        item = {"key":q["key"],"desc":q["desc"],"target":q["target"],
                "reward":q["reward"],"progress":0,"claimed":False}
        if q["key"] == "win_streak": item["start_streak"] = u.win_streak
        u.quests.append(item)
    u.last_quest_reset = time.time()

async def ensure_quests(u: User) -> None:
    if time.time() - u.last_quest_reset > QUEST_RESET_COOLDOWN or not u.quests:
        roll_quests(u)
        await persist_extra(u)

def update_quests(u: User, kind: str, amount: int = 1) -> list[str]:
    done = []
    for q in u.quests:
        if q.get("claimed"): continue
        if q["key"] == kind == "win_streak":
            q["progress"] = max(0, u.win_streak - q.get("start_streak", 0))
        elif q["key"] == kind:
            q["progress"] = q.get("progress", 0) + amount
        if q.get("progress", 0) >= q["target"] and not q.get("_logged"):
            done.append(q["desc"]); q["_logged"] = True
    return done

def refresh_streak_quest(u: User) -> None:
    for q in u.quests:
        if q["key"] == "win_streak" and not q.get("claimed"):
            q["progress"] = max(0, u.win_streak - q.get("start_streak", 0))

def reset_streak_quest(u: User) -> None:
    for q in u.quests:
        if q["key"] == "win_streak" and not q.get("claimed"):
            q["progress"] = 0; q["start_streak"] = u.win_streak

async def award_ach(uid: int, names: list[str]) -> int:
    if not names: return 0
    total = 0
    async with ulock(uid):
        u = await _read(uid)
        if u is None: return 0
        for k, a in ACHIEVEMENTS.items():
            tag = f"{a['name']} (+{a['reward']} CF)"
            if tag in names and k not in u.achievements:
                u.achievements.append(k); total += a["reward"]
        if total > 0:
            async with tx():
                await _update_balance(uid, total, total)
                await _touch_tx(uid, "reward", total, "Достижения")
                await persist_extra(u)
    return total

# ============================== GAMES ==============================
@dataclass(frozen=True)
class Game:
    key: str; name: str; emoji: str; multiplier: float; desc: str; row: int = 0

GAMES = {
    "coinflip": Game("coinflip","CoinFlip","🪙",2,"Орёл/Решка. x2.",0),
    "dice": Game("dice","Dice","🎲",5.5,"Число 1–6. x5.5.",0),
    "mines": Game("mines","Mines","💣",0,"5×5, 3 мины.",1),
    "wheel": Game("wheel","Колесо","🎡",2,"Красное/Чёрное x2, Зелёное x35.",1),
    "bowling": Game("bowling","Боулинг","🎳",2,"Страйк/промах. x2.",2),
    "darts": Game("darts","Дартс","🎯",2,"Яблочко/мимо. x2.",2),
    "football": Game("football","Футбол","⚽",2,"Гол/мимо. x2.",3),
    "basketball": Game("basketball","Баскетбол","🏀",2,"Попал/промах. x2.",3),
    "slots": Game("slots","Слоты","🎰",0,"Собери три в ряд.",4),
    "ladder": Game("ladder","Лестница","🪜",0,"Растущий множитель.",5),
    "double": Game("double","Дабл","2️⃣",2,"50/50. x2.",5),
    "jackpot": Game("jackpot","Джекпот","💎",0,"Шанс x100 (1%).",6),
}

# ============================== KEYBOARDS ==============================
def kb_main() -> InlineKeyboardMarkup:
    rows: dict[int, list] = {}
    for g in GAMES.values():
        rows.setdefault(g.row, []).append(InlineKeyboardButton(
            text=f"{g.emoji} {g.name}" + (f" (x{g.multiplier:g})" if g.multiplier else ""),
            callback_data=f"game:{g.key}"))
    m = [rows[i] for i in sorted(rows)]
    m.append([InlineKeyboardButton(text="👤 Профиль", callback_data="menu:profile"),
              InlineKeyboardButton(text="🏆 Топ", callback_data="menu:top")])
    m.append([InlineKeyboardButton(text="💰 Баланс", callback_data="menu:balance"),
              InlineKeyboardButton(text="💸 Вывод", callback_data="menu:withdraw")])
    m.append([InlineKeyboardButton(text="📥 Пополнение", callback_data="menu:deposit")])
    m.append([InlineKeyboardButton(text="🎁 Казна", callback_data="menu:treasury"),
              InlineKeyboardButton(text="🎯 Дейли", callback_data="menu:daily")])
    m.append([InlineKeyboardButton(text="⏰ Часовой", callback_data="menu:hourly"),
              InlineKeyboardButton(text="📅 Недельный", callback_data="menu:weekly")])
    m.append([InlineKeyboardButton(text="📢 Награды", callback_data="menu:rewards"),
              InlineKeyboardButton(text="🏅 Достижения", callback_data="menu:ach")])
    m.append([InlineKeyboardButton(text="📊 История", callback_data="menu:history"),
              InlineKeyboardButton(text="👥 Реферал", callback_data="menu:ref")])
    m.append([InlineKeyboardButton(text="📋 Квесты", callback_data="menu:quests"),
              InlineKeyboardButton(text="💠 VIP", callback_data="menu:vip")])
    m.append([InlineKeyboardButton(text="🛡 Клан", callback_data="menu:clan"),
              InlineKeyboardButton(text="⚔️ Дуэли", callback_data="menu:duel")])
    m.append([InlineKeyboardButton(text="🛒 Магазин", callback_data="menu:shop"),
              InlineKeyboardButton(text="🏆 Турнир", callback_data="menu:tournament")])
    m.append([InlineKeyboardButton(text="📈 Статистика", callback_data="menu:stats"),
              InlineKeyboardButton(text="ℹ️ Правила", callback_data="menu:rules")])
    return InlineKeyboardMarkup(inline_keyboard=m)

def kb_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="menu:main"),
         InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]])

def kb_admin_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ В админ-панель", callback_data="admin:panel"),
         InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main")]])

def kb_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Выдать", callback_data="admin:give"),
         InlineKeyboardButton(text="➖ Забрать", callback_data="admin:take")],
        [InlineKeyboardButton(text="🚫 Бан", callback_data="admin:ban"),
         InlineKeyboardButton(text="✅ Разбан", callback_data="admin:unban")],
        [InlineKeyboardButton(text="🎟 Промокод", callback_data="admin:promo_new"),
         InlineKeyboardButton(text="📢 Рассылка", callback_data="admin:broadcast")],
        [InlineKeyboardButton(text="🏅 VIP", callback_data="admin:vip"),
         InlineKeyboardButton(text="🏆 Турнир", callback_data="admin:tournament")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats")],
        [InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main")]])

def kb_deposit() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Telegram Stars", callback_data="dep:stars")],
        [InlineKeyboardButton(text="🎨 NFT (TON)", callback_data="dep:nft")],
        [InlineKeyboardButton(text="💳 Карта / Крипта", callback_data="dep:manual")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="menu:main")]])

def kb_withdraw() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 TON (Keeper)", callback_data="wd:ton")],
        [InlineKeyboardButton(text="💳 Карта МИР", callback_data="wd:mir")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="menu:main")]])

def kb_top() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Баланс", callback_data="top:balance"),
         InlineKeyboardButton(text="🏅 Победы", callback_data="top:wins")],
        [InlineKeyboardButton(text="🔥 Стрик", callback_data="top:streak"),
         InlineKeyboardButton(text="🏆 Турнир", callback_data="top:tournament")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="menu:main")]])

def kb_rewards(u: User) -> InlineKeyboardMarkup:
    r = []
    for k, info in SUBSCRIBE_REWARDS.items():
        c = k in u.subscribed_channels
        t = f"✅ {info['chat_id']}" if c else f"🎁 {info['chat_id']} (+{info['reward']})"
        r.append([InlineKeyboardButton(text=t, callback_data=f"rew:{k}")])
    r.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=r)

def kb_vip(u: User) -> InlineKeyboardMarkup:
    r = []
    if not u.is_vip:
        r.append([InlineKeyboardButton(text=f"💠 За {VIP_PRICE_CF} CF", callback_data="vip:cf")])
        r.append([InlineKeyboardButton(text=f"⭐ За {VIP_PRICE_STARS} Stars", callback_data="vip:stars")])
    r.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=r)

def kb_clan(u: User) -> InlineKeyboardMarkup:
    r = []
    if u.clan_id is None:
        r.append([InlineKeyboardButton(text=f"🛡 Создать ({CLAN_CREATE_PRICE} CF)", callback_data="clan:create")])
        r.append([InlineKeyboardButton(text="🔍 Вступить", callback_data="clan:join_list")])
    else:
        r.append([InlineKeyboardButton(text="🏰 Мой клан", callback_data="clan:my")])
    r.append([InlineKeyboardButton(text="🏆 Топ кланов", callback_data="clan:top")])
    r.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=r)

def kb_shop() -> InlineKeyboardMarkup:
    r = [[InlineKeyboardButton(text=f"{i['name']} — {i['price']} CF", callback_data=f"shop:{k}")]
         for k, i in SHOP_ITEMS.items()]
    r.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=r)

def kb_nft() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Через блокчейн TON", callback_data="nft:chain")],
        [InlineKeyboardButton(text="👤 Через аккаунт Telegram", callback_data="nft:account")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="menu:deposit")]])

# ============================== BOT ==============================
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

class St(StatesGroup):
    coinflip=State(); dice=State(); mines=State(); wheel=State()
    bowling=State(); darts=State(); football=State(); basketball=State()
    slots=State(); ladder=State(); double=State(); jackpot=State()
    wd_dest=State(); wd_amt=State()
    a_give_t=State(); a_give_a=State(); a_take_t=State(); a_take_a=State()
    a_ban=State(); a_unban=State(); a_promo_a=State(); a_promo_u=State()
    a_cast=State(); a_vip_t=State(); a_vip_d=State()
    clan_name=State(); duel_amt=State()

async def edit(msg: Message, text: str, kb: InlineKeyboardMarkup) -> None:
    if msg is None: return
    try:
        await msg.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        with suppress(TelegramAPIError):
            await msg.answer(text, reply_markup=kb)
    except TelegramAPIError: pass

class BanMW(BaseMiddleware):
    async def __call__(self, handler, event, data):
        from_user = getattr(event, "from_user", None)
        if from_user is None: return await handler(event, data)
        u = await get_user(from_user.id, from_user.username or "")
        if u.banned:
            if isinstance(event, Message):
                with suppress(TelegramAPIError): await event.answer("🚫 Ты забанен.")
            elif isinstance(event, CallbackQuery):
                with suppress(TelegramAPIError): await event.answer("🚫 Забанен.", show_alert=True)
            return
        data["user"] = u
        return await handler(event, data)

dp.message.middleware(BanMW())
dp.callback_query.middleware(BanMW())

@dp.error()
async def on_error(event: ErrorEvent):
    if isinstance(event.exception, TelegramRetryAfter):
        await asyncio.sleep(event.exception.retry_after)
        return True
    log.exception("handler error: %s", event.exception)
    return True

# ============================== TEXTS ==============================
def txt_profile(u: User) -> str:
    cur, need, nxt = u.next_level()
    prog = f"📈 До {esc(nxt)}: <b>{fmt_big(cur)}</b> / {fmt_big(need)}" if need > cur else "📈 <b>МАКСИМУМ</b>"
    vip = f"💠 VIP до {time.strftime('%d.%m %H:%M', time.localtime(u.vip_until))}" if u.is_vip else "💠 VIP нет"
    luck = f"\n🍀 Талисман до {time.strftime('%d.%m %H:%M', time.localtime(u.luck_charm_until))}" if u.has_luck_charm else ""
    shield = f"\n🛡 Щитов: {u.streak_shield}" if u.streak_shield else ""
    return (
        f"👤 <b>Профиль</b> {esc(u.level())}\n\n"
        f"🆔 <code>{u.user_id}</code>\n📛 {udisp(u)}\n📅 {fmt_age(time.time()-u.registered_at)}{shield}\n\n"
        f"🪙 <b>{fmt_big(u.balance)} CF</b> ({fmt_rub(u.balance)})\n"
        f"🎁 Бонусных: <b>{fmt_big(u.bonus_balance)}</b>\n"
        f"✅ К выводу: <b>{fmt_big(u.paid_balance)}</b>\n\n"
        f"🎮 Игр: {u.total_games} | 🏅 {u.total_wins}\n"
        f"🔥 Стрик: {u.win_streak} | Лучший: {u.best_win_streak}\n"
        f"💵 Оборот: {fmt_big(u.total_wagered)}\n{prog}\n{vip}{luck}\n\n"
        f"📥 Депозитов: {u.total_deposits_cf} | 📤 Выведено: {u.total_withdraws_cf}\n"
        f"👥 Рефералов: {u.referral_count}"
    )

def txt_rules() -> str:
    lines = ["ℹ️ <b>Правила</b>\n", f"• 1 CF = {COIN_RATE:.2f} ₽\n"]
    for g in GAMES.values():
        lines.append(f"• {g.emoji} <b>{esc(g.name)}</b>: {esc(g.desc)}")
    lines += ["", f"• Мин. вывод: {MIN_WITHDRAW_CF} | Макс: {MAX_WITHDRAW_CF} CF",
              "• Вывод: TON или Карта МИР", "",
              "⚠️ <b>Важно:</b>", "🪙 Заработанное и задоначенное — выводится",
              "🎁 Бонусное — нельзя выводить", "",
              f"🎁 <b>Казна:</b> 90 мин, {TREASURY_MIN}–{TREASURY_MAX}",
              f"🎯 <b>Дейли:</b> 24 ч", f"⏰ <b>Часовой:</b> +{HOURLY_REWARD}",
              f"📅 <b>Недельный:</b> +{WEEKLY_REWARD}",
              f"💠 <b>VIP:</b> {VIP_PRICE_CF} CF или {VIP_PRICE_STARS} ⭐, кэшбэк {int(VIP_CASHBACK*100)}%",
              f"🛡 <b>Клан:</b> {CLAN_CREATE_PRICE} CF, кэшбэк {int(CLAN_CASHBACK*100)}%",
              "", f"⚠️ <b>Оплата строго на мой аккаунт</b> {esc(DEPOSIT_CONTACT)}",
              f"📥 Минимум: {MIN_DEPOSIT_RUB} ₽"]
    return "\n".join(lines)

def txt_history(u: User) -> str:
    if not u.transactions: return "📊 <b>История</b>\n\nПусто."
    emo = {"game_win":"✅","game_lose":"❌","deposit":"📥","withdraw":"📤","treasury":"🎁",
           "daily":"🎯","reward":"🏅","promo":"🎟","referral":"👥","admin_give":"➕",
           "admin_take":"➖","hourly":"⏰","weekly":"📅","vip":"💠","duel_win":"⚔️",
           "duel_lose":"💀","quest":"📋","shop":"🛒","clan":"🛡","tournament":"🏆",
           "cashback":"💸"}
    lines = ["📊 <b>Последние операции</b>\n"]
    for t in reversed(u.transactions[-20:]):
        e = emo.get(t.get("kind",""),"•"); amt = t.get("amount",0)
        s = "+" if amt > 0 else ""
        ts = t.get("ts",0); ts_s = time.strftime("%d.%m %H:%M", time.localtime(ts)) if ts else "—"
        note = f" — {esc(t['note'])}" if t.get("note") else ""
        lines.append(f"{e} <code>{ts_s}</code> {s}{amt} CF{note}")
    return "\n".join(lines)

def txt_ach(u: User) -> str:
    lines = ["🏅 <b>Достижения</b>\n"]
    for k, a in ACHIEVEMENTS.items():
        m = "✅" if k in u.achievements else "⬜"
        lines.append(f"{m} <b>{esc(a['name'])}</b> (+{a['reward']})")
    return "\n".join(lines) + f"\n\n📊 {len(u.achievements)}/{len(ACHIEVEMENTS)}"

def txt_quests(u: User) -> str:
    lines = ["📋 <b>Квесты</b>\n",
             f"🔄 Сброс через <b>{fmt_cd(int(QUEST_RESET_COOLDOWN-(time.time()-u.last_quest_reset)))}</b>\n"]
    for q in u.quests:
        if q.get("claimed"): m = "✅"
        elif q.get("progress",0) >= q["target"]: m = "🎁"
        else: m = "⬜"
        p = min(q.get("progress",0), q["target"])
        lines.append(f"{m} <b>{esc(q['desc'])}</b>\n   {p}/{q['target']} | +{q['reward']}")
    return "\n".join(lines)

def txt_stats(u: User) -> str:
    wr = (u.total_wins/u.total_games*100) if u.total_games else 0
    avg = (u.total_wagered//u.total_games) if u.total_games else 0
    return (f"📈 <b>Статистика</b>\n\n🎮 Игр: <b>{u.total_games}</b>\n"
            f"🏅 Побед: <b>{u.total_wins}</b>\n📊 Win-rate: <b>{wr:.1f}%</b>\n\n"
            f"💵 Оборот: <b>{fmt_big(u.total_wagered)}</b>\n🎯 Средняя: <b>{avg}</b>\n"
            f"🔥 Стрик: <b>{u.best_win_streak}</b>\n💣 Mines клеток: <b>{u.mines_opened_total}</b>\n\n"
            f"🎁 Казны: <b>{u.treasury_count}</b>\n👥 Рефералов: <b>{u.referral_count}</b>\n"
            f"🏅 Достижений: <b>{len(u.achievements)}/{len(ACHIEVEMENTS)}</b>")

# ============================== COMMON HANDLERS ==============================
@dp.message(CommandStart())
async def on_start(msg: Message, state: FSMContext):
    await state.clear()
    if msg.from_user is None: return
    u = await get_user(msg.from_user.id, msg.from_user.username or "")
    await ensure_quests(u)
    args = (msg.text or "").split()
    if len(args) >= 2 and args[1].startswith("ref_"):
        with suppress(ValueError):
            rid = int(args[1].split("_",1)[1])
            if rid != u.user_id:
                a, b = sorted([rid, u.user_id])
                async with ulock(a):
                    async with ulock(b):
                        uu = await _read(u.user_id); rr = await _read(rid)
                        if uu and rr and uu.referrer_id is None and rr.referrer_id != uu.user_id:
                            uu.referrer_id = rid
                            rr.referral_count += 1
                            async with tx():
                                await _update_balance(rid, REFERRAL_BONUS_REFERRER, REFERRAL_BONUS_REFERRER)
                                await _update_balance(u.user_id, REFERRAL_BONUS_NEW, REFERRAL_BONUS_NEW)
                                await DB.execute("UPDATE users SET referrer_id=?, referral_count=referral_count+1 WHERE user_id=?",
                                                 (rid, u.user_id))
                                await DB.execute("UPDATE users SET referral_count=? WHERE user_id=?",
                                                 (rr.referral_count, rid))
                                await _touch_tx(rid, "referral", REFERRAL_BONUS_REFERRER, "Реферал")
                                await _touch_tx(u.user_id, "referral", REFERRAL_BONUS_NEW, "Бонус за старт")
                with suppress(TelegramAPIError):
                    await bot.send_message(rid, f"👥 +{REFERRAL_BONUS_REFERRER} CF за реферала!")
    text = (f"🪙 <b>coinflip</b>\n\nБаланс: <b>{fmt_big(u.balance)} CF</b> ({fmt_rub(u.balance)})\n"
            f"1 CF = {COIN_RATE:.2f} ₽ | Мин: {MIN_BET} | Макс: {MAX_BET}\n\n"
            f"💡 <i>казна, бонус, час, неделя</i>\n\nИгр: {len(GAMES)}. Выбирай:")
    await msg.answer(text, reply_markup=kb_main())

@dp.message(Command("help"))
async def on_help(msg: Message):
    await msg.answer("ℹ️ <b>Команды</b>\n\n/start /me /top /promo &lt;код&gt; /cancel /help\n\n"
                     "💡 казна, бонус, час, неделя")

@dp.message(Command("cancel"))
async def on_cancel(msg: Message, state: FSMContext):
    if await state.get_state() is None:
        await msg.answer("Нечего отменять."); return
    await state.clear()
    await msg.answer("Отменено.", reply_markup=kb_main())

@dp.callback_query(F.data == "cancel")
async def cb_cancel(call: CallbackQuery, state: FSMContext):
    if call.message is None: await call.answer(); return
    await state.clear()
    await edit(call.message, "Отменено.", kb_main())
    await call.answer()

@dp.message(Command("me"))
async def on_me(msg: Message, user: User):
    await msg.answer(txt_profile(user))

@dp.message(Command("top"))
async def on_top(msg: Message):
    await msg.answer(await render_top("balance"), reply_markup=kb_top())

@dp.message(Command("promo"))
async def on_promo(msg: Message, user: User):
    args = (msg.text or "").split()
    if len(args) < 2: await msg.answer("/promo &lt;код&gt;"); return
    code = args[1].strip().upper()
    async with glock("promos"):
        async with ulock(user.user_id):
            promos = await kv_get("promos", {})
            p = promos.get(code)
            if not p: await msg.answer("❌ Не найден."); return
            u = await _read(user.user_id)
            if u is None: return
            if code in u.promos_used: await msg.answer("❌ Уже использован."); return
            if p.get("uses_left",0) <= 0: await msg.answer("❌ Исчерпан."); return
            p["uses_left"] -= 1; promos[code] = p
            await kv_set("promos", promos)
            u.promos_used.append(code)
            async with tx():
                await _update_balance(u.user_id, p["reward"], p["reward"] if p.get("bonus_only",True) else 0)
                await _touch_tx(u.user_id, "promo", p["reward"], f"Промокод {code}")
                await persist_extra(u)
    await msg.answer(f"🎟 <b>+{p['reward']} CF</b>")

@dp.callback_query(F.data == "menu:main")
async def cb_main(call: CallbackQuery, state: FSMContext, user: User):
    await state.clear()
    if call.message is None: await call.answer(); return
    await edit(call.message, f"🪙 <b>coinflip</b>\n\nБаланс: <b>{fmt_big(user.balance)} CF</b>\n\nВыбери игру:", kb_main())
    await call.answer()

@dp.callback_query(F.data == "menu:balance")
async def cb_balance(call: CallbackQuery, user: User):
    if call.message is None: await call.answer(); return
    await edit(call.message, f"💰 <b>Баланс</b>\n\nCF: <b>{user.balance}</b>\nРубли: <b>{fmt_rub(user.balance)}</b>\n\n"
                              f"🎁 Бонусных: <b>{user.bonus_balance}</b>\n✅ К выводу: <b>{user.paid_balance}</b>", kb_back())
    await call.answer()

@dp.callback_query(F.data == "menu:rules")
async def cb_rules(call: CallbackQuery):
    if call.message is None: await call.answer(); return
    await edit(call.message, txt_rules(), kb_back()); await call.answer()

@dp.callback_query(F.data == "menu:history")
async def cb_history(call: CallbackQuery, user: User):
    if call.message is None: await call.answer(); return
    await edit(call.message, txt_history(user), kb_back()); await call.answer()

@dp.callback_query(F.data == "menu:ach")
async def cb_ach(call: CallbackQuery, user: User):
    if call.message is None: await call.answer(); return
    await edit(call.message, txt_ach(user), kb_back()); await call.answer()

@dp.callback_query(F.data == "menu:stats")
async def cb_stats(call: CallbackQuery, user: User):
    if call.message is None: await call.answer(); return
    await edit(call.message, txt_stats(user), kb_back()); await call.answer()

@dp.callback_query(F.data == "menu:profile")
async def cb_profile(call: CallbackQuery, user: User):
    if call.message is None: await call.answer(); return
    await edit(call.message, txt_profile(user), kb_back()); await call.answer()

async def render_top(kind: str) -> str:
    fm = {"balance":("balance",TOPLIST_MIN_BALANCE),"wins":("wins",0),
          "streak":("streak",0),"tournament":("tournament",0)}
    key, minv = fm.get(kind, ("balance", TOPLIST_MIN_BALANCE))
    top = await top_users(key, TOPLIST_SIZE, minv)
    if not top: return "🏆 <b>Топ</b>\n\nПусто."
    titles = {"balance":"по балансу","wins":"по победам","streak":"по стрику","tournament":"турнира"}
    lines = [f"🏆 <b>Топ-{TOPLIST_SIZE} {titles.get(kind,'')}</b>\n"]
    med = ["🥇","🥈","🥉"]
    for i, u in enumerate(top):
        p = med[i] if i < 3 else f"{i+1}."
        if kind == "balance": val = f"<b>{fmt_big(u.balance)} CF</b>"
        elif kind == "wins": val = f"<b>{u.total_wins}</b>"
        elif kind == "streak": val = f"🔥 <b>{u.best_win_streak}</b>"
        else: val = f"🏆 <b>{fmt_big(u.tournament_score)}</b>"
        lines.append(f"{p} {udisp(u)} — {val}")
    return "\n".join(lines)

@dp.callback_query(F.data == "menu:top")
async def cb_top(call: CallbackQuery):
    if call.message is None: await call.answer(); return
    await edit(call.message, await render_top("balance"), kb_top()); await call.answer()

@dp.callback_query(F.data.startswith("top:"))
async def cb_top_k(call: CallbackQuery):
    if call.message is None: await call.answer(); return
    k = call.data.split(":",1)[1]
    if k not in ("balance","wins","streak","tournament"):
        await call.answer("Нет.", show_alert=True); return
    await edit(call.message, await render_top(k), kb_top()); await call.answer()

# ============================== BONUSES ==============================
_treasury_msg_cd: dict[int, float] = {}

async def do_bonus(uid: int, username: str, chat_type: str, kind: str) -> Optional[str]:
    async with ulock(uid):
        u = await _get_locked(uid, username)
        if u.banned: return None
        now = time.time()
        if kind == "treasury":
            w = u.wait_treasury()
            if w > 0:
                return (f"⏳ <b>Казна ещё не открылась</b>\n\n"
                        f"⏱ Осталось: <b>{fmt_cd(w)}</b>\n\n"
                        f"💡 Казна открывается раз в {TREASURY_COOLDOWN // 60} минут.")
            amt = roll_treasury()
            async with tx():
                await _update_balance(uid, amt, amt)
                await DB.execute("UPDATE users SET last_treasury_at=?, treasury_count=treasury_count+1 WHERE user_id=?", (now, uid))
                await _touch_tx(uid, "treasury", amt, "Казна")
            u.last_treasury_at = now; u.treasury_count += 1
            u.balance += amt; u.bonus_balance += amt
            update_quests(u, "treasury_3", 1)
            ach = check_ach(u)
            if ach: await award_ach(uid, ach)
            await persist_extra(u)
            ach_line = f"\n\n🏅 {', '.join(ach)}" if ach else ""
            return (f"🎁 <b>Казна открылась!</b>\n\n"
                    f"💰 Получено: <b>+{amt} CF</b> ({fmt_rub(amt)}){ach_line}\n\n"
                    f"⏱ Следующая через <b>{TREASURY_COOLDOWN // 60} мин</b>\n"
                    f"💼 Баланс: <b>{u.balance} CF</b>")
        if kind == "daily":
            w = u.wait_daily()
            if w > 0:
                return (f"⏳ <b>Дейли уже забран</b>\n\n"
                        f"⏱ Осталось: <b>{fmt_cd(w)}</b>\n"
                        f"🔥 Стрик: <b>{u.daily_streak}</b>")
            if (now-u.last_daily_at) > (DAILY_COOLDOWN+24*3600) and u.streak_shield > 0 and u.last_daily_at > 0:
                u.streak_shield -= 1; cont = True
            else:
                cont = (now-u.last_daily_at) < (DAILY_COOLDOWN+24*3600) and u.last_daily_at > 0
            u.daily_streak = u.daily_streak+1 if cont else 1
            base = RNG.choice(DAILY_REWARDS); sb = DAILY_STREAK_BONUS*(u.daily_streak-1); tot = base+sb
            async with tx():
                await _update_balance(uid, tot, tot)
                await DB.execute("UPDATE users SET last_daily_at=?, daily_streak=?, streak_shield=? WHERE user_id=?",
                                 (now, u.daily_streak, u.streak_shield, uid))
                await _touch_tx(uid, "daily", tot, f"Стрик {u.daily_streak}")
            u.balance += tot; u.bonus_balance += tot; u.last_daily_at = now
            ach = check_ach(u)
            if ach: await award_ach(uid, ach)
            await persist_extra(u)
            ach_line = f"\n\n🏅 {', '.join(ach)}" if ach else ""
            return (f"🎯 <b>Ежедневный бонус!</b>\n\n"
                    f"💰 Получено: <b>+{tot} CF</b>\n"
                    f"🔥 Стрик: <b>{u.daily_streak}</b> дн.{ach_line}")
        if kind == "hourly":
            w = u.wait_hourly()
            if w > 0:
                return f"⏰ <b>Часовой уже забран</b>\n\n⏱ Осталось: <b>{fmt_cd(w)}</b>"
            async with tx():
                await _update_balance(uid, HOURLY_REWARD, HOURLY_REWARD)
                await DB.execute("UPDATE users SET last_hourly_at=? WHERE user_id=?", (now, uid))
                await _touch_tx(uid, "hourly", HOURLY_REWARD, "Часовой")
            return f"⏰ <b>+{HOURLY_REWARD} CF</b>"
        if kind == "weekly":
            w = u.wait_weekly()
            if w > 0:
                return f"📅 <b>Недельный уже забран</b>\n\n⏱ Осталось: <b>{fmt_cd(w)}</b>"
            async with tx():
                await _update_balance(uid, WEEKLY_REWARD, WEEKLY_REWARD)
                await DB.execute("UPDATE users SET last_weekly_at=? WHERE user_id=?", (now, uid))
                await _touch_tx(uid, "weekly", WEEKLY_REWARD, "Недельный")
            return f"📅 <b>+{WEEKLY_REWARD} CF</b>"
        return None

@dp.message(Command("казна"))
@dp.message(F.text.func(is_treasury_cmd))
async def cmd_treasury(msg: Message):
    if msg.from_user is None: return
    uid = msg.from_user.id
    now = time.time()
    if now - _treasury_msg_cd.get(uid, 0) < TREASURY_MSG_LIMIT:
        return
    _treasury_msg_cd[uid] = now
    r = await do_bonus(uid, msg.from_user.username or "", msg.chat.type, "treasury")
    if not r: return
    if msg.chat.type == "private":
        await msg.answer(r)
    elif "Казна открылась" in r:
        await msg.reply(f"🎁 {umention(uid, msg.from_user.username or '')} забрал казну!")

@dp.message(Command("бонус"))
@dp.message(F.text.func(is_daily_cmd))
async def cmd_daily(msg: Message):
    if msg.from_user is None: return
    r = await do_bonus(msg.from_user.id, msg.from_user.username or "", msg.chat.type, "daily")
    if not r: return
    if msg.chat.type == "private":
        await msg.answer(r)
    elif "Получено" in r:
        await msg.reply(f"🎯 {umention(msg.from_user.id, msg.from_user.username or '')} +бонус!")

@dp.message(Command("час"))
@dp.message(F.text.func(is_hourly_cmd))
async def cmd_hourly(msg: Message):
    if msg.from_user is None: return
    r = await do_bonus(msg.from_user.id, msg.from_user.username or "", msg.chat.type, "hourly")
    if not r: return
    if msg.chat.type == "private":
        await msg.answer(r)
    elif "+" in r:
        await msg.reply(f"⏰ {umention(msg.from_user.id, msg.from_user.username or '')} +{HOURLY_REWARD} CF!")

@dp.message(Command("неделя"))
@dp.message(F.text.func(is_weekly_cmd))
async def cmd_weekly(msg: Message):
    if msg.from_user is None: return
    r = await do_bonus(msg.from_user.id, msg.from_user.username or "", msg.chat.type, "weekly")
    if not r: return
    if msg.chat.type == "private":
        await msg.answer(r)
    elif "+" in r:
        await msg.reply(f"📅 {umention(msg.from_user.id, msg.from_user.username or '')} +{WEEKLY_REWARD} CF!")

@dp.callback_query(F.data == "menu:treasury")
async def cb_treasury(call: CallbackQuery, user: User):
    if call.message is None: await call.answer(); return
    w = user.wait_treasury()
    txt = (f"🎁 <b>Казна</b>\n\n⏱ Через <b>{fmt_cd(w)}</b>.\n\n💡 Напиши «<b>казна</b>» в чат."
           if w > 0 else "🎁 <b>Казна готова!</b>\n\n💡 Напиши «<b>казна</b>» в чат.")
    await edit(call.message, txt, kb_back()); await call.answer()

@dp.callback_query(F.data == "menu:daily")
async def cb_daily(call: CallbackQuery, user: User):
    if call.message is None: await call.answer(); return
    w = user.wait_daily()
    txt = (f"🎯 <b>Дейли</b>\nЧерез <b>{fmt_cd(w)}</b>.\nСтрик: {user.daily_streak}"
           if w > 0 else "🎯 <b>Дейли готов!</b>\nНапиши «бонус» в чат.")
    await edit(call.message, txt, kb_back()); await call.answer()

@dp.callback_query(F.data == "menu:hourly")
async def cb_hourly(call: CallbackQuery, user: User):
    if call.message is None: await call.answer(); return
    w = user.wait_hourly()
    txt = f"⏰ <b>Часовой</b>\n" + (f"Через <b>{fmt_cd(w)}</b>." if w > 0 else "Напиши «час» в чат.")
    await edit(call.message, txt, kb_back()); await call.answer()

@dp.callback_query(F.data == "menu:weekly")
async def cb_weekly(call: CallbackQuery, user: User):
    if call.message is None: await call.answer(); return
    w = user.wait_weekly()
    txt = f"📅 <b>Недельный</b>\n" + (f"Через <b>{fmt_cd(w)}</b>." if w > 0 else "Напиши «неделя» в чат.")
    await edit(call.message, txt, kb_back()); await call.answer()

# ============================== QUESTS ==============================
@dp.callback_query(F.data == "menu:quests")
async def cb_quests(call: CallbackQuery, user: User):
    if call.message is None: await call.answer(); return
    await ensure_quests(user)
    await edit(call.message, txt_quests(user), kb_back()); await call.answer()

@dp.callback_query(F.data.startswith("q:"))
async def cb_quest_claim(call: CallbackQuery, user: User):
    if call.message is None: await call.answer(); return
    try: idx = int(call.data.split(":")[1])
    except (ValueError, IndexError): await call.answer(); return
    async with ulock(user.user_id):
        u = await _read(user.user_id)
        if u is None or idx < 0 or idx >= len(u.quests):
            await call.answer("Неверный индекс.", show_alert=True); return
        q = u.quests[idx]
        if q.get("claimed") or q.get("progress",0) < q["target"]:
            await call.answer("Не выполнен.", show_alert=True); return
        q["claimed"] = True
        async with tx():
            await _update_balance(u.user_id, q["reward"], q["reward"])
            await _touch_tx(u.user_id, "quest", q["reward"], q["desc"])
            await persist_extra(u)
    await call.answer(f"📋 +{q['reward']} CF!", show_alert=True)
    await edit(call.message, txt_quests(u), kb_back())

# ============================== REWARDS ==============================
@dp.callback_query(F.data == "menu:rewards")
async def cb_rewards(call: CallbackQuery, user: User):
    if call.message is None: await call.answer(); return
    await edit(call.message, f"📢 <b>Награды</b>\nМаксимум: <b>{SUBSCRIBE_TOTAL} CF</b>", kb_rewards(user))
    await call.answer()

@dp.callback_query(F.data.startswith("rew:"))
async def cb_reward(call: CallbackQuery, user: User):
    if call.message is None: await call.answer(); return
    key = call.data.split(":",1)[1]
    info = SUBSCRIBE_REWARDS.get(key)
    if not info: await call.answer("Ошибка.", show_alert=True); return
    if key in user.subscribed_channels:
        await call.answer("Уже получено.", show_alert=True); return
    if not await is_subscribed(bot, user.user_id, info["chat_id"]):
        await call.answer(f"❌ Не подписан на {info['chat_id']}.", show_alert=True); return
    async with ulock(user.user_id):
        u = await _read(user.user_id)
        if u is None or key in u.subscribed_channels:
            await call.answer("Уже получено.", show_alert=True); return
        u.subscribed_channels.append(key)
        async with tx():
            await _update_balance(u.user_id, info["reward"], info["reward"])
            await _touch_tx(u.user_id, "reward", info["reward"], f"Подписка {info['chat_id']}")
            await persist_extra(u)
        ach = check_ach(u)
        if ach: await award_ach(u.user_id, ach)
    await call.answer(f"🎁 +{info['reward']} CF!", show_alert=True)
    await edit(call.message, f"📢 <b>+{info['reward']} CF</b> за {esc(info['chat_id'])}", kb_rewards(u))

# ============================== VIP ==============================
@dp.callback_query(F.data == "menu:vip")
async def cb_vip(call: CallbackQuery, user: User):
    if call.message is None: await call.answer(); return
    if user.is_vip:
        until = time.strftime("%d.%m.%Y %H:%M", time.localtime(user.vip_until))
        txt = f"💠 <b>VIP активен</b> до {until}\nКэшбэк: {int(VIP_CASHBACK*100)}%"
    else:
        txt = (f"💠 <b>VIP</b>\n\nЦена: <b>{VIP_PRICE_CF} CF</b> или <b>{VIP_PRICE_STARS} ⭐</b>\n"
               f"Срок: 7 дней\nКэшбэк: {int(VIP_CASHBACK*100)}% от проигрышей")
    await edit(call.message, txt, kb_vip(user)); await call.answer()

@dp.callback_query(F.data == "vip:cf")
async def cb_vip_cf(call: CallbackQuery, user: User):
    if call.message is None: await call.answer(); return
    async with ulock(user.user_id):
        u = await _read(user.user_id)
        if u is None: return
        if u.is_vip: await call.answer("Уже есть.", show_alert=True); return
        if u.balance < VIP_PRICE_CF: await call.answer(f"Нужно {VIP_PRICE_CF} CF.", show_alert=True); return
        async with tx():
            await DB.execute(
                "UPDATE users SET balance=balance-?, vip_until=MAX(vip_until,?)+? WHERE user_id=?",
                (VIP_PRICE_CF, time.time(), VIP_DURATION, u.user_id))
            await _touch_tx(u.user_id, "vip", -VIP_PRICE_CF, "VIP за CF")
            u.balance -= VIP_PRICE_CF
            u.vip_until = max(u.vip_until, time.time()) + VIP_DURATION
            ach = check_ach(u)
            if ach: await award_ach(u.user_id, ach)
    await call.answer("💠 VIP активирован!", show_alert=True)
    until = time.strftime("%d.%m.%Y %H:%M", time.localtime(u.vip_until))
    await edit(call.message, f"💠 VIP до {until}", kb_back())

@dp.callback_query(F.data == "vip:stars")
async def cb_vip_stars(call: CallbackQuery, user: User):
    if call.message is None: await call.answer(); return
    txt = (
        f"💠 <b>Покупка VIP за Stars</b>\n\n"
        f"⚠️ <b>Отправь звёзды/подарок строго на мой аккаунт</b> {esc(DEPOSIT_CONTACT)}\n\n"
        f"<b>Как это сделать:</b>\n"
        f"1. Открой мой профиль {esc(DEPOSIT_CONTACT)}\n"
        f"2. Отправь мне <b>{VIP_PRICE_STARS} ⭐</b> (подарком или звёздами)\n"
        f"3. Напиши мне: «VIP» + свой ID <code>{user.user_id}</code>\n\n"
        f"Я проверю оплату и активирую VIP вручную на 7 дней.\n\n"
        f"💠 Что даёт VIP:\n"
        f"• Кэшбэк {int(VIP_CASHBACK*100)}% от проигрышей\n"
        f"• Значок в профиле"
    )
    await edit(call.message, txt, kb_back()); await call.answer()

# ============================== SHOP ==============================
@dp.callback_query(F.data == "menu:shop")
async def cb_shop(call: CallbackQuery):
    if call.message is None: await call.answer(); return
    txt = "🛒 <b>Магазин</b>\n\n" + "\n".join(
        f"• <b>{esc(i['name'])}</b> — {i['price']} CF\n   {esc(i['desc'])}" for i in SHOP_ITEMS.values())
    await edit(call.message, txt, kb_shop()); await call.answer()

@dp.callback_query(F.data.startswith("shop:"))
async def cb_shop_buy(call: CallbackQuery, user: User):
    if call.message is None: await call.answer(); return
    key = call.data.split(":",1)[1]
    item = SHOP_ITEMS.get(key)
    if not item: await call.answer("Нет.", show_alert=True); return
    async with ulock(user.user_id):
        u = await _read(user.user_id)
        if u is None: return
        if u.balance < item["price"]:
            await call.answer(f"❌ Нужно {item['price']}.", show_alert=True); return
        async with tx():
            await DB.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (item["price"], u.user_id))
            await _touch_tx(u.user_id, "shop", -item["price"], f"Покупка {item['name']}")
        now = time.time(); msg_txt = "Куплено"
        if key == "bonus_1000":
            async with tx():
                await _update_balance(u.user_id, 1000, 1000)
                await _touch_tx(u.user_id, "shop", 1000, "Бонус 1000")
            msg_txt = "+1000 CF"
        elif key == "bonus_5000":
            async with tx():
                await _update_balance(u.user_id, 5000, 5000)
                await _touch_tx(u.user_id, "shop", 5000, "Бонус 5000")
            msg_txt = "+5000 CF"
        elif key == "streak_shield":
            await DB.execute("UPDATE users SET streak_shield=streak_shield+1 WHERE user_id=?", (u.user_id,))
            await DB.commit(); msg_txt = "Щит +1"
        elif key == "luck_charm":
            await DB.execute("UPDATE users SET luck_charm_until=MAX(luck_charm_until,?)+? WHERE user_id=?",
                             (now, 24*3600, u.user_id))
            await DB.commit(); msg_txt = "Талисман 24ч"
        elif key == "xp_boost":
            await DB.execute("UPDATE users SET total_wagered=total_wagered+1000 WHERE user_id=?", (u.user_id,))
            await DB.commit(); msg_txt = "+1000 оборот"
        u2 = await _read(u.user_id); ach = check_ach(u2) if u2 else []
        if ach: await award_ach(u.user_id, ach)
    await call.answer("🛒 Куплено!", show_alert=True)
    await edit(call.message, f"🛒 <b>{esc(item['name'])}</b>\n\n✅ {msg_txt}", kb_shop())

# ============================== CLAN ==============================
@dp.callback_query(F.data == "menu:clan")
async def cb_clan(call: CallbackQuery, user: User):
    if call.message is None: await call.answer(); return
    txt = f"🛡 <b>Кланы</b>\n\nСоздай за <b>{CLAN_CREATE_PRICE} CF</b>, кэшбэк {int(CLAN_CASHBACK*100)}%"
    await edit(call.message, txt, kb_clan(user)); await call.answer()

@dp.callback_query(F.data == "clan:create")
async def cb_clan_create(call: CallbackQuery, state: FSMContext, user: User):
    if call.message is None: await call.answer(); return
    if user.clan_id is not None: await call.answer("Уже в клане.", show_alert=True); return
    if user.balance < CLAN_CREATE_PRICE: await call.answer(f"Нужно {CLAN_CREATE_PRICE}.", show_alert=True); return
    await state.set_state(St.clan_name)
    await edit(call.message, "🛡 Название клана (2–20 символов):", kb_back()); await call.answer()

@dp.message(St.clan_name)
async def on_clan_name(msg: Message, state: FSMContext, user: User):
    if msg.text is None: return
    name = msg.text.strip()
    if not NAME_RE.match(name):
        await msg.answer("❌ 2–20 символов: буквы, цифры, пробел, - _"); return
    async with glock("clans"):
        async with tx():
            async with DB.execute("SELECT clan_id FROM clans WHERE LOWER(name)=LOWER(?)", (name,)) as cur:
                if await cur.fetchone():
                    await msg.answer("❌ Имя занято."); return
            u = await _read(user.user_id)
            if u is None or u.balance < CLAN_CREATE_PRICE:
                await msg.answer("❌ Мало CF."); await state.clear(); return
            cur = await DB.execute("INSERT INTO clans(name,owner_id,created_at) VALUES(?,?,?)",
                                    (name, u.user_id, time.time()))
            cid = cur.lastrowid
            await DB.execute("INSERT INTO clan_members(clan_id,user_id,joined_at) VALUES(?,?,?)",
                             (cid, u.user_id, time.time()))
            await DB.execute("UPDATE users SET balance=balance-?, clan_id=? WHERE user_id=?",
                             (CLAN_CREATE_PRICE, cid, u.user_id))
            await _touch_tx(u.user_id, "clan", -CLAN_CREATE_PRICE, f"Клан {name}")
    await state.clear()
    await msg.answer(f"✅ <b>Клан «{esc(name)}» создан!</b>", reply_markup=kb_main())

@dp.callback_query(F.data == "clan:join_list")
async def cb_clan_join_list(call: CallbackQuery):
    if call.message is None: await call.answer(); return
    async with DB.execute(
        "SELECT c.clan_id, c.name, COUNT(m.user_id) AS cnt FROM clans c "
        "LEFT JOIN clan_members m ON m.clan_id=c.clan_id GROUP BY c.clan_id HAVING cnt < ?",
        (CLAN_MAX_MEMBERS,)) as cur:
        rows = await cur.fetchall()
    if not rows:
        await edit(call.message, "🛡 Пока нет кланов.", kb_back()); await call.answer(); return
    kbd = [[InlineKeyboardButton(text=f"🛡 {esc(r['name'])} ({r['cnt']}/{CLAN_MAX_MEMBERS})",
                                  callback_data=f"clan:join:{r['clan_id']}")] for r in rows]
    kbd.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu:clan")])
    await edit(call.message, "🛡 <b>Кланы:</b>", InlineKeyboardMarkup(inline_keyboard=kbd))
    await call.answer()

@dp.callback_query(F.data.startswith("clan:join:"))
async def cb_clan_join(call: CallbackQuery, user: User):
    if call.message is None: await call.answer(); return
    try: cid = int(call.data.split(":",2)[2])
    except (ValueError, IndexError): await call.answer(); return
    async with glock("clans"):
        async with tx():
            async with DB.execute("SELECT COUNT(*) AS c FROM clan_members WHERE clan_id=?", (cid,)) as cur:
                row = await cur.fetchone()
            if row["c"] >= CLAN_MAX_MEMBERS:
                await call.answer("Клан полон.", show_alert=True); return
            u = await _read(user.user_id)
            if u is None or u.clan_id is not None:
                await call.answer("Уже в клане.", show_alert=True); return
            try:
                await DB.execute("INSERT INTO clan_members(clan_id,user_id,joined_at) VALUES(?,?,?)",
                                 (cid, u.user_id, time.time()))
            except aiosqlite.IntegrityError:
                await call.answer("Уже в списке.", show_alert=True); return
            await DB.execute("UPDATE users SET clan_id=? WHERE user_id=?", (cid, u.user_id))
    await call.answer("🛡 Вступил!", show_alert=True)
    await edit(call.message, "✅ В клане.", kb_back())

@dp.callback_query(F.data == "clan:my")
async def cb_clan_my(call: CallbackQuery, user: User):
    if call.message is None: await call.answer(); return
    if user.clan_id is None: await call.answer("Не в клане.", show_alert=True); return
    async with DB.execute("SELECT * FROM clans WHERE clan_id=?", (user.clan_id,)) as cur:
        c = await cur.fetchone()
    if not c:
        await DB.execute("UPDATE users SET clan_id=NULL WHERE user_id=?", (user.user_id,))
        await DB.commit()
        await call.answer("Клан исчез.", show_alert=True); return
    async with DB.execute("SELECT user_id FROM clan_members WHERE clan_id=?", (user.clan_id,)) as cur:
        members = [r["user_id"] for r in await cur.fetchall()]
    owner = await get_user(c["owner_id"])
    txt = (f"🛡 <b>Клан «{esc(c['name'])}»</b>\n\n👑 {udisp(owner)}\n"
           f"👥 {len(members)}/{CLAN_MAX_MEMBERS}\nКэшбэк: {int(CLAN_CASHBACK*100)}%\n\nУчастники:")
    for mid in members:
        m = await get_user(mid); txt += f"\n• {udisp(m)}"
    rows = []
    if c["owner_id"] == user.user_id:
        rows.append([InlineKeyboardButton(text="🗑 Удалить", callback_data="clan:del")])
    else:
        rows.append([InlineKeyboardButton(text="🚪 Выйти", callback_data="clan:leave")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu:clan")])
    await edit(call.message, txt, InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()

@dp.callback_query(F.data == "clan:leave")
async def cb_clan_leave(call: CallbackQuery, user: User):
    if call.message is None: await call.answer(); return
    if user.clan_id is None: await call.answer("Не в клане.", show_alert=True); return
    async with glock("clans"):
        async with tx():
            await DB.execute("DELETE FROM clan_members WHERE clan_id=? AND user_id=?",
                             (user.clan_id, user.user_id))
            await DB.execute("UPDATE users SET clan_id=NULL WHERE user_id=?", (user.user_id,))
    await call.answer("Вышел.", show_alert=True)
    await edit(call.message, "Вышел из клана.", kb_back())

@dp.callback_query(F.data == "clan:del")
async def cb_clan_del(call: CallbackQuery, user: User):
    if call.message is None: await call.answer(); return
    if user.clan_id is None: await call.answer("Не в клане.", show_alert=True); return
    async with glock("clans"):
        async with DB.execute("SELECT owner_id FROM clans WHERE clan_id=?", (user.clan_id,)) as cur:
            row = await cur.fetchone()
        if not row or row["owner_id"] != user.user_id:
            await call.answer("Только лидер.", show_alert=True); return
        async with tx():
            await DB.execute("UPDATE users SET clan_id=NULL WHERE clan_id=?", (user.clan_id,))
            await DB.execute("DELETE FROM clan_members WHERE clan_id=?", (user.clan_id,))
            await DB.execute("DELETE FROM clans WHERE clan_id=?", (user.clan_id,))
    await call.answer("Удалён.", show_alert=True)
    await edit(call.message, "Клан удалён.", kb_back())

@dp.callback_query(F.data == "clan:top")
async def cb_clan_top(call: CallbackQuery):
    if call.message is None: await call.answer(); return
    async with DB.execute(
        "SELECT c.name, COUNT(m.user_id) AS cnt FROM clans c "
        "LEFT JOIN clan_members m ON m.clan_id=c.clan_id "
        "GROUP BY c.clan_id ORDER BY cnt DESC LIMIT 10") as cur:
        rows = await cur.fetchall()
    if not rows:
        await edit(call.message, "🛡 Нет кланов.", kb_back()); await call.answer(); return
    lines = ["🏆 <b>Топ кланов</b>\n"]
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. <b>{esc(r['name'])}</b> — {r['cnt']}")
    await edit(call.message, "\n".join(lines), kb_back()); await call.answer()

# ============================== DUELS ==============================
@dp.callback_query(F.data == "menu:duel")
async def cb_duel(call: CallbackQuery):
    if call.message is None: await call.answer(); return
    async with DB.execute("SELECT COUNT(*) AS c FROM duels WHERE status='open'") as cur:
        row = await cur.fetchone()
    txt = f"⚔️ <b>Дуэли</b>\n\nОткрытых: <b>{row['c']}</b>"
    rows = [[InlineKeyboardButton(text="⚔️ Создать", callback_data="duel:create")]]
    if row["c"] > 0:
        rows.append([InlineKeyboardButton(text="🔍 Список", callback_data="duel:list")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu:main")])
    await edit(call.message, txt, InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()

@dp.callback_query(F.data == "duel:create")
async def cb_duel_create(call: CallbackQuery, state: FSMContext):
    if call.message is None: await call.answer(); return
    await state.set_state(St.duel_amt)
    await edit(call.message, "⚔️ Введи сумму ставки:", kb_back()); await call.answer()

@dp.message(St.duel_amt)
async def on_duel_amt(msg: Message, state: FSMContext, user: User):
    if msg.text is None: return
    bet, err = parse_bet(msg.text, user)
    if err or bet is None:
        await msg.answer(err or "❌"); return
    async with tx():
        await DB.execute("INSERT INTO duels(creator_id,amount,status,created_at) VALUES(?,?,'open',?)",
                         (user.user_id, bet, time.time()))
    await state.clear()
    await msg.answer(f"⚔️ <b>Дуэль создана! Ставка: {bet} CF</b>", reply_markup=kb_main())

@dp.callback_query(F.data == "duel:list")
async def cb_duel_list(call: CallbackQuery, user: User):
    if call.message is None: await call.answer(); return
    async with DB.execute(
        "SELECT * FROM duels WHERE status='open' AND creator_id != ? ORDER BY created_at DESC LIMIT 10",
        (user.user_id,)) as cur:
        rows = await cur.fetchall()
    if not rows:
        await edit(call.message, "⚔️ Нет дуэлей.", kb_back()); await call.answer(); return
    kbd = []
    for d in rows:
        c = await get_user(d["creator_id"])
        kbd.append([InlineKeyboardButton(text=f"⚔️ {udisp(c)} — {d['amount']} CF",
                                          callback_data=f"duel:acc:{d['duel_id']}")])
    kbd.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu:duel")])
    await edit(call.message, "⚔️ <b>Дуэли:</b>", InlineKeyboardMarkup(inline_keyboard=kbd))
    await call.answer()

@dp.callback_query(F.data.startswith("duel:acc:"))
async def cb_duel_acc(call: CallbackQuery, user: User):
    if call.message is None or call.from_user is None: await call.answer(); return
    try: did = int(call.data.split(":",2)[2])
    except (ValueError, IndexError): await call.answer(); return
    async with tx():
        cur = await DB.execute(
            "UPDATE duels SET status='processing', opponent_id=? "
            "WHERE duel_id=? AND status='open' AND creator_id != ?",
            (user.user_id, did, user.user_id))
        await DB.commit()
        if cur.rowcount != 1:
            await call.answer("Уже принята.", show_alert=True); return
    async with DB.execute("SELECT * FROM duels WHERE duel_id=?", (did,)) as cur:
        d = await cur.fetchone()
    creator_id = d["creator_id"]; amount = d["amount"]
    a, b = sorted([creator_id, user.user_id])
    winner_id = None
    async with ulock(a):
        async with ulock(b):
            c_u = await _read(creator_id); o_u = await _read(user.user_id)
            if c_u is None or o_u is None or c_u.balance < amount or o_u.balance < amount:
                async with tx():
                    await DB.execute("UPDATE duels SET status='open', opponent_id=NULL WHERE duel_id=?", (did,))
                await call.answer("❌ Недостаточно CF.", show_alert=True); return
            winner_id = RNG.choice([creator_id, user.user_id])
            w, l = (c_u, o_u) if winner_id == creator_id else (o_u, c_u)
            async with tx():
                await _update_balance(w.user_id, amount, 0)
                await _update_balance(l.user_id, -amount, 0)
                await DB.execute("UPDATE users SET total_wins=total_wins+1, "
                                 "win_streak=win_streak+1 WHERE user_id=?", (w.user_id,))
                await DB.execute("UPDATE users SET best_win_streak=MAX(best_win_streak,win_streak) WHERE user_id=?",
                                 (w.user_id,))
                await DB.execute("UPDATE users SET win_streak=0 WHERE user_id=?", (l.user_id,))
                await _touch_tx(w.user_id, "duel_win", amount, f"Дуэль #{did}")
                await _touch_tx(l.user_id, "duel_lose", -amount, f"Дуэль #{did}")
                await DB.execute("UPDATE duels SET status='finished', winner_id=?, finished_at=? WHERE duel_id=?",
                                 (winner_id, time.time(), did))
    wu = await get_user(winner_id)
    await call.answer("Победа!" if winner_id == user.user_id else "Проиграл :(")
    await edit(call.message, f"⚔️ <b>Дуэль #{did}</b>\n\nПобедил {udisp(wu)}! +{amount} CF", kb_main())

# ============================== GAME ENGINE ==============================
Choice = dict[str, tuple[str, int]]
@dataclass
class Runtime:
    state: State
    choices: Choice
    kb_builder: Any

def choice_kb(gk: str, ch: Choice) -> InlineKeyboardMarkup:
    btns = [InlineKeyboardButton(text=lab, callback_data=f"g:{gk}:{s}") for s,(lab,_) in ch.items()]
    rows = [btns[i:i+2] for i in range(0, len(btns), 2)]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

RUNTIME: dict[str, Runtime] = {
    "coinflip": Runtime(St.coinflip,
        {"h":("🪙 Орёл",1),"t":("🪙 Решка",1)},
        lambda: choice_kb("coinflip",{"h":("Орёл",1),"t":("Решка",1)})),
    "dice": Runtime(St.dice,
        {str(i):(str(i),1) for i in range(1,7)},
        lambda: InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=str(i),callback_data=f"g:dice:{i}") for i in (1,2,3)],
            [InlineKeyboardButton(text=str(i),callback_data=f"g:dice:{i}") for i in (4,5,6)],
            [InlineKeyboardButton(text="◀️ Назад",callback_data="menu:main")]])),
    "wheel": Runtime(St.wheel,
        {"r":("🔴 Красное",18),"b":("⚫ Чёрное",18),"g":("🟢 Зелёное",1)},
        lambda: InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔴 Красное (x2)",callback_data="g:wheel:r"),
             InlineKeyboardButton(text="⚫ Чёрное (x2)",callback_data="g:wheel:b")],
            [InlineKeyboardButton(text="🟢 Зелёное (x35)",callback_data="g:wheel:g")],
            [InlineKeyboardButton(text="◀️ Назад",callback_data="menu:main")]])),
    "bowling": Runtime(St.bowling,{"s":("🎳 Страйк",1),"m":("🚫 Промах",1)},
        lambda: choice_kb("bowling",{"s":("🎳 Страйк",1),"m":("🚫 Промах",1)})),
    "darts": Runtime(St.darts,{"b":("🎯 В яблочко",1),"m":("❌ Мимо",1)},
        lambda: choice_kb("darts",{"b":("🎯 В яблочко",1),"m":("❌ Мимо",1)})),
    "football": Runtime(St.football,{"g":("⚽ Гол",1),"m":("🚫 Мимо",1)},
        lambda: choice_kb("football",{"g":("⚽ Гол",1),"m":("🚫 Мимо",1)})),
    "basketball": Runtime(St.basketball,{"h":("🏀 Попал",1),"m":("🚫 Промах",1)},
        lambda: choice_kb("basketball",{"h":("🏀 Попал",1),"m":("🚫 Промах",1)})),
}

def game_mult(gk: str, choice: str) -> float:
    if gk == "wheel" and choice == "g": return 35.0
    return GAMES[gk].multiplier

async def run_choice(call: CallbackQuery, state: FSMContext, gk: str, choice: str, bet: int, user: User):
    if call.message is None or call.from_user is None: return
    rt = RUNTIME[gk]
    opts = list(rt.choices.keys()); ws = [rt.choices[o][1] for o in opts]
    result = RNG.choices(opts, weights=ws, k=1)[0]
    win = choice == result
    mult = game_mult(gk, choice)
    game = GAMES[gk]
    u = await _read(user.user_id)
    if u is None: return
    from_bonus = u.bonus_balance >= bet
    if win:
        async with ulock(u.user_id):
            async with tx():
                uu = await _read(u.user_id)
                if uu is None or uu.balance < bet:
                    await call.answer("Недостаточно CF.", show_alert=True); return
                bp = min(uu.bonus_balance, bet)
                gain = int(bet * (mult - 1))
                await DB.execute(
                    "UPDATE users SET balance=balance-?+?, bonus_balance=bonus_balance-?+?, "
                    "total_games=total_games+1, total_wins=total_wins+1, "
                    "win_streak=win_streak+1, total_wagered=total_wagered+? WHERE user_id=?",
                    (bet, gain, bp, gain if from_bonus else 0, bet, u.user_id))
                await DB.execute(
                    "UPDATE users SET best_win_streak=MAX(best_win_streak,win_streak) WHERE user_id=?",
                    (u.user_id,))
                await _touch_tx(u.user_id, "game_win", gain, f"{game.name} x{mult}")
    else:
        cb = await apply_loss(u.user_id, bet, "game_lose", game.name)
        if cb < 0:
            await call.answer("Недостаточно CF.", show_alert=True); return
    u = await _read(user.user_id)
    if u is None: return
    update_quests(u, "play_games", 1)
    if win: update_quests(u, "win_games", 1)
    else: reset_streak_quest(u)
    update_quests(u, "bet_1000", bet)
    refresh_streak_quest(u)
    ach = check_ach(u)
    if ach: await award_ach(u.user_id, ach)
    await persist_extra(u)
    await state.clear()
    rl = rt.choices[result][0]; cl = rt.choices[choice][0]
    mention = umention(call.from_user.id, call.from_user.username or "")
    ach_line = ("\n\n🏅 " + ", ".join(ach)) if ach else ""
    res = f"✅ +{int(bet*(mult-1))} CF" if win else f"❌ -{bet} CF"
    txt = (f"{game.emoji} {mention} — <b>{esc(rl)}</b>\nВыбор: {esc(cl)}\n\n"
           f"{res}{ach_line}\n\nБаланс: <b>{u.balance} CF</b> ({fmt_rub(u.balance)})")
    await edit(call.message, txt, kb_main())
    await call.answer("Победа!" if win else "Увы...")

@dp.callback_query(F.data.startswith("game:"))
async def cb_start(call: CallbackQuery, state: FSMContext, user: User):
    if call.message is None: await call.answer(); return
    key = call.data.split(":",1)[1]
    game = GAMES.get(key)
    if not game: await call.answer("Нет.", show_alert=True); return
    if key == "mines": return await start_mines(call, state, user)
    if key == "ladder": return await start_ladder(call, state, user)
    if key == "slots": return await start_slots(call, state, user)
    if key == "double": return await start_double(call, state, user)
    if key == "jackpot": return await start_jackpot(call, state, user)
    rt = RUNTIME.get(key)
    if not rt: await call.answer("Недоступно.", show_alert=True); return
    await state.set_state(rt.state)
    mention = umention(call.from_user.id, call.from_user.username or "")
    await edit(call.message, f"{game.emoji} <b>{esc(game.name)}</b> — {mention}\n\n"
                              f"{esc(game.desc)}\n\nВведи ставку:", kb_back())
    await call.answer()

async def generic_bet(msg: Message, state: FSMContext, user: User):
    if msg.text is None: return
    cur = await state.get_state()
    gk = next((k for k, rt in RUNTIME.items() if cur and cur.endswith(rt.state.state)), None)
    if not gk: return
    bet, err = parse_bet(msg.text, user)
    if err or bet is None:
        if msg.chat.type == "private": await msg.answer(err or "❌")
        return
    await state.update_data(bet=bet, gk=gk, owner=user.user_id)
    mention = umention(user.user_id, user.username)
    await msg.answer(f"🎯 {mention}, ставка <b>{bet} CF</b>\nВыбор:",
                     reply_markup=RUNTIME[gk].kb_builder())

for _k, _rt in RUNTIME.items():
    dp.message(_rt.state)(generic_bet)

@dp.callback_query(F.data.startswith("g:"))
async def cb_choice(call: CallbackQuery, state: FSMContext, user: User):
    if call.message is None: await call.answer(); return
    parts = call.data.split(":",2)
    if len(parts) != 3: await call.answer(); return
    _, gk, choice = parts
    rt = RUNTIME.get(gk)
    if not rt or choice not in rt.choices: await call.answer("Ошибка.", show_alert=True); return
    data = await state.get_data()
    bet = data.get("bet"); owner = data.get("owner")
    if owner is not None and owner != user.user_id:
        await call.answer("Не твоя.", show_alert=True); return
    if not bet: await call.answer("Сессия.", show_alert=True); return
    await run_choice(call, state, gk, choice, bet, user)

# ============================== MINES ==============================
def mines_mult(opened: int) -> float:
    if opened <= 0: return 1.0
    total = MINES_SIZE * MINES_SIZE; safe = total - MINES_COUNT
    p = 1.0
    for i in range(opened):
        p *= (safe - i) / (total - i)
    return round(0.95 / p, 2) if p > 0 else 0

def mines_kb(opened: list, bet: int) -> InlineKeyboardMarkup:
    rows = []
    for r in range(MINES_SIZE):
        row = []
        for c in range(MINES_SIZE):
            i = r * MINES_SIZE + c
            if i in opened: row.append(InlineKeyboardButton(text="💎", callback_data="mines:skip"))
            else: row.append(InlineKeyboardButton(text="⬜", callback_data=f"mines:o:{i}"))
        rows.append(row)
    if opened:
        m = mines_mult(len(opened))
        rows.append([InlineKeyboardButton(text=f"💰 Забрать {int(bet*m)} CF (x{m})", callback_data="mines:cash")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def mines_text(opened: list, bet: int) -> str:
    m = mines_mult(len(opened))
    return f"💣 <b>Mines</b>\nМин: {MINES_COUNT} | Открыто: {len(opened)}\nx{m} | Заберёшь: {int(bet*m)} CF"

async def start_mines(call: CallbackQuery, state: FSMContext, user: User):
    await state.set_state(St.mines)
    if call.message is not None:
        mention = umention(user.user_id, user.username)
        await edit(call.message, f"💣 <b>Mines</b> — {mention}\nВведи ставку:", kb_back())
    await call.answer()

@dp.message(St.mines)
async def bet_mines(msg: Message, state: FSMContext, user: User):
    if msg.text is None: return
    bet, err = parse_bet(msg.text, user)
    if err or bet is None:
        if msg.chat.type == "private": await msg.answer(err or "❌")
        return
    cells = list(range(MINES_SIZE * MINES_SIZE))
    mines = set(RNG.sample(cells, MINES_COUNT))
    await state.update_data(bet=bet, opened=[], mines=list(mines), owner=user.user_id)
    await msg.answer(mines_text([], bet), reply_markup=mines_kb([], bet))

@dp.callback_query(F.data == "mines:skip")
async def mines_skip(call: CallbackQuery): await call.answer()

@dp.callback_query(F.data.startswith("mines:o:"))
async def mines_open(call: CallbackQuery, state: FSMContext, user: User):
    if call.message is None: await call.answer(); return
    data = await state.get_data()
    bet = data.get("bet"); opened = list(data.get("opened", []))
    mines = data.get("mines", []); owner = data.get("owner")
    if owner is not None and owner != user.user_id:
        await call.answer("Не твоя.", show_alert=True); return
    if not bet or not mines: await call.answer("Сессия.", show_alert=True); return
    try: idx = int(call.data.split(":")[2])
    except (ValueError, IndexError): await call.answer(); return
    if idx in opened: await call.answer(); return
    if idx in mines:
        cb = await apply_loss(user.user_id, bet, "game_lose", "Mines")
        if cb < 0: await call.answer("Недостаточно.", show_alert=True); return
        u = await _read(user.user_id)
        update_quests(u, "play_games", 1); reset_streak_quest(u)
        ach = check_ach(u)
        if ach: await award_ach(user.user_id, ach)
        await persist_extra(u)
        await state.clear()
        rows = []
        for r in range(MINES_SIZE):
            row = []
            for c in range(MINES_SIZE):
                i = r * MINES_SIZE + c
                t = "💥" if i in mines else ("💎" if i in opened else "⬜")
                row.append(InlineKeyboardButton(text=t, callback_data="mines:skip"))
            rows.append(row)
        rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main")])
        await edit(call.message, f"💥 <b>БУМ!</b>\n-{bet} CF\nБаланс: <b>{u.balance}</b>",
                   InlineKeyboardMarkup(inline_keyboard=rows))
        await call.answer("Мина!", show_alert=True); return
    opened.append(idx)
    await state.update_data(opened=opened)
    await edit(call.message, mines_text(opened, bet), mines_kb(opened, bet))
    await call.answer(RNG.choice(["💎","Чисто!","Ещё"]))

@dp.callback_query(F.data == "mines:cash")
async def mines_cash(call: CallbackQuery, state: FSMContext, user: User):
    if call.message is None: await call.answer(); return
    data = await state.get_data()
    bet = data.get("bet"); opened = data.get("opened", []); owner = data.get("owner")
    if owner is not None and owner != user.user_id:
        await call.answer("Не твоя.", show_alert=True); return
    if not bet or not opened: await call.answer("Нечего.", show_alert=True); return
    m = mines_mult(len(opened)); payout = int(bet * m)
    u = await _read(user.user_id)
    if u is None: return
    async with ulock(u.user_id):
        async with tx():
            uu = await _read(u.user_id)
            if uu is None or uu.balance < bet:
                await call.answer("Недостаточно.", show_alert=True); return
            bp = min(uu.bonus_balance, bet)
            gain = payout - bet
            await DB.execute(
                "UPDATE users SET balance=balance-?+?, bonus_balance=bonus_balance-?, "
                "total_games=total_games+1, total_wins=total_wins+1, "
                "win_streak=win_streak+1, total_wagered=total_wagered+?, "
                "mines_opened_total=mines_opened_total+? WHERE user_id=?",
                (bet, gain, bp, bet, len(opened), u.user_id))
            await DB.execute(
                "UPDATE users SET best_win_streak=MAX(best_win_streak,win_streak) WHERE user_id=?",
                (u.user_id,))
            await _touch_tx(u.user_id, "game_win", gain, f"Mines x{m}")
    u = await _read(user.user_id)
    update_quests(u, "play_games", 1); update_quests(u, "win_games", 1)
    update_quests(u, "mines_open", len(opened))
    update_quests(u, "bet_1000", bet); refresh_streak_quest(u)
    ach = check_ach(u)
    if ach: await award_ach(u.user_id, ach)
    await persist_extra(u)
    await state.clear()
    ach_line = ("\n\n🏅 " + ", ".join(ach)) if ach else ""
    await edit(call.message, f"💰 <b>Забрал!</b>\nx{m} | +{gain} CF{ach_line}\n\n"
                              f"Баланс: <b>{u.balance}</b>", kb_main())
    await call.answer("Забрал!")

# ============================== SLOTS ==============================
SLOT_SYMS = ["🍒","🍋","🍇","⭐","💎","7️⃣"]
def slots_mult(spin: list) -> float:
    if spin[0] == spin[1] == spin[2]:
        if spin[0] == "7️⃣": return 50
        if spin[0] == "💎": return 30
        return 10
    if spin[0] == spin[1] or spin[1] == spin[2] or spin[0] == spin[2]: return 1
    return 0

async def start_slots(call: CallbackQuery, state: FSMContext, user: User):
    await state.set_state(St.slots)
    if call.message is not None:
        mention = umention(user.user_id, user.username)
        await edit(call.message, f"🎰 <b>Слоты</b> — {mention}\nВведи ставку:", kb_back())
    await call.answer()

@dp.message(St.slots)
async def bet_slots(msg: Message, state: FSMContext, user: User):
    if msg.text is None: return
    bet, err = parse_bet(msg.text, user)
    if err or bet is None:
        if msg.chat.type == "private": await msg.answer(err or "❌")
        return
    await state.update_data(bet=bet, owner=user.user_id)
    await msg.answer(f"🎰 Ставка <b>{bet} CF</b>",
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                         [InlineKeyboardButton(text="🎰 Крутить", callback_data="slots:spin")],
                         [InlineKeyboardButton(text="◀️ Назад", callback_data="menu:main")]]))

@dp.callback_query(F.data == "slots:spin")
async def slots_spin(call: CallbackQuery, state: FSMContext, user: User):
    if call.message is None: await call.answer(); return
    data = await state.get_data()
    bet = data.get("bet"); owner = data.get("owner")
    if owner is not None and owner != user.user_id:
        await call.answer("Не твоя.", show_alert=True); return
    if not bet: await call.answer("Сессия.", show_alert=True); return
    spin = [RNG.choice(SLOT_SYMS) for _ in range(3)]
    m = slots_mult(spin); win = m > 1; refund = m == 1
    if refund:
        u = await _read(user.user_id)
        async with ulock(u.user_id):
            async with tx():
                await DB.execute("UPDATE users SET total_games=total_games+1, total_wagered=total_wagered+? WHERE user_id=?",
                                 (bet, u.user_id))
                await _touch_tx(u.user_id, "game_lose", 0, "Slots возврат")
        await state.clear()
        await edit(call.message, f"🎰 <b>{' | '.join(spin)}</b>\n\n🔄 Возврат ставки\n\n"
                                  f"Баланс: <b>{u.balance}</b>", kb_main())
        await call.answer("Возврат"); return
    if win:
        async with ulock(user.user_id):
            async with tx():
                uu = await _read(user.user_id)
                if uu is None or uu.balance < bet: return
                bp = min(uu.bonus_balance, bet)
                gain = int(bet * (m - 1))
                await DB.execute(
                    "UPDATE users SET balance=balance-?+?, bonus_balance=bonus_balance-?, "
                    "total_games=total_games+1, total_wins=total_wins+1, "
                    "win_streak=win_streak+1, total_wagered=total_wagered+? WHERE user_id=?",
                    (bet, gain, bp, bet, uu.user_id))
                await DB.execute("UPDATE users SET best_win_streak=MAX(best_win_streak,win_streak) WHERE user_id=?",
                                 (uu.user_id,))
                await _touch_tx(uu.user_id, "game_win", gain, f"Slots x{m}")
    else:
        cb = await apply_loss(user.user_id, bet, "game_lose", "Slots")
        if cb < 0: await call.answer("Недостаточно.", show_alert=True); return
    u = await _read(user.user_id)
    update_quests(u, "play_games", 1)
    if win: update_quests(u, "win_games", 1)
    else: reset_streak_quest(u)
    update_quests(u, "bet_1000", bet)
    if win: refresh_streak_quest(u)
    ach = check_ach(u)
    if ach: await award_ach(u.user_id, ach)
    await persist_extra(u)
    await state.clear()
    ach_line = ("\n\n🏅 " + ", ".join(ach)) if ach else ""
    res = f"✅ +{int(bet*(m-1))} CF (x{m:g})" if win else f"❌ -{bet} CF"
    await edit(call.message, f"🎰 <b>{' | '.join(spin)}</b>\n\n{res}{ach_line}\n\n"
                              f"Баланс: <b>{u.balance}</b>", kb_main())
    await call.answer("Джекпот!" if m >= 10 else ("+" if win else "Мимо"))

# ============================== LADDER ==============================
LADDER_STEPS = 10
def ladder_mult(step: int) -> float:
    return round(1.3 ** step, 2) if step > 0 else 1.0

def ladder_kb(cur: int, bet: int) -> InlineKeyboardMarkup:
    m = ladder_mult(cur); nx = ladder_mult(cur + 1)
    rows = []
    if cur < LADDER_STEPS:
        rows.append([InlineKeyboardButton(text=f"➡️ Вверх (x{nx})", callback_data="ladder:up")])
    if cur > 0:
        rows.append([InlineKeyboardButton(text=f"💰 Забрать {int(bet*m)} CF (x{m})", callback_data="ladder:cash")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def start_ladder(call: CallbackQuery, state: FSMContext, user: User):
    await state.set_state(St.ladder)
    if call.message is not None:
        mention = umention(user.user_id, user.username)
        await edit(call.message, f"🪜 <b>Лестница</b> — {mention}\nВведи ставку:", kb_back())
    await call.answer()

@dp.message(St.ladder)
async def bet_ladder(msg: Message, state: FSMContext, user: User):
    if msg.text is None: return
    bet, err = parse_bet(msg.text, user)
    if err or bet is None:
        if msg.chat.type == "private": await msg.answer(err or "❌")
        return
    await state.update_data(bet=bet, step=0, owner=user.user_id)
    await msg.answer(f"🪜 Шаг 0/{LADDER_STEPS}\nx1", reply_markup=ladder_kb(0, bet))

@dp.callback_query(F.data == "ladder:up")
async def ladder_up(call: CallbackQuery, state: FSMContext, user: User):
    if call.message is None: await call.answer(); return
    data = await state.get_data()
    bet = data.get("bet"); step = data.get("step", 0); owner = data.get("owner")
    if owner is not None and owner != user.user_id:
        await call.answer("Не твоя.", show_alert=True); return
    if not bet: await call.answer("Сессия.", show_alert=True); await state.clear(); return
    if RNG.random() < 0.25:
        cb = await apply_loss(user.user_id, bet, "game_lose", f"Ladder упал {step}")
        if cb < 0: await call.answer("Недостаточно.", show_alert=True); return
        u = await _read(user.user_id)
        update_quests(u, "play_games", 1); reset_streak_quest(u)
        ach = check_ach(u)
        if ach: await award_ach(user.user_id, ach)
        await persist_extra(u)
        await state.clear()
        await edit(call.message, f"🪜 <b>Упал!</b> Шаг {step}\n-{bet} CF\nБаланс: <b>{u.balance}</b>", kb_main())
        await call.answer("Упал!", show_alert=True); return
    step += 1
    if step >= LADDER_STEPS:
        m = ladder_mult(step); payout = int(bet * m)
        async with ulock(user.user_id):
            async with tx():
                uu = await _read(user.user_id)
                if uu is None or uu.balance < bet: return
                bp = min(uu.bonus_balance, bet)
                gain = payout - bet
                await DB.execute(
                    "UPDATE users SET balance=balance-?+?, bonus_balance=bonus_balance-?, "
                    "total_games=total_games+1, total_wins=total_wins+1, "
                    "win_streak=win_streak+1, total_wagered=total_wagered+? WHERE user_id=?",
                    (bet, gain, bp, bet, uu.user_id))
                await DB.execute("UPDATE users SET best_win_streak=MAX(best_win_streak,win_streak) WHERE user_id=?",
                                 (uu.user_id,))
                await _touch_tx(uu.user_id, "game_win", gain, f"Ladder x{m}")
        u = await _read(user.user_id)
        update_quests(u, "play_games", 1); update_quests(u, "win_games", 1)
        update_quests(u, "bet_1000", bet); refresh_streak_quest(u)
        ach = check_ach(u)
        if ach: await award_ach(u.user_id, ach)
        await persist_extra(u)
        await state.clear()
        ach_line = ("\n\n🏅 " + ", ".join(ach)) if ach else ""
        await edit(call.message, f"🏆 <b>Дошёл!</b> x{m}\n+{payout-bet} CF{ach_line}\n\n"
                                  f"Баланс: <b>{u.balance}</b>", kb_main())
        await call.answer("Победа!", show_alert=True); return
    await state.update_data(step=step)
    m = ladder_mult(step)
    await edit(call.message, f"🪜 Шаг {step}/{LADDER_STEPS}\nx{m}", ladder_kb(step, bet))
    await call.answer("Вверх!")

@dp.callback_query(F.data == "ladder:cash")
async def ladder_cash(call: CallbackQuery, state: FSMContext, user: User):
    if call.message is None: await call.answer(); return
    data = await state.get_data()
    bet = data.get("bet"); step = data.get("step", 0); owner = data.get("owner")
    if owner is not None and owner != user.user_id:
        await call.answer("Не твоя.", show_alert=True); return
    if not bet or step == 0: await call.answer("Нечего.", show_alert=True); return
    m = ladder_mult(step); payout = int(bet * m)
    async with ulock(user.user_id):
        async with tx():
            uu = await _read(user.user_id)
            if uu is None or uu.balance < bet: return
            bp = min(uu.bonus_balance, bet)
            gain = payout - bet
            await DB.execute(
                "UPDATE users SET balance=balance-?+?, bonus_balance=bonus_balance-?, "
                "total_games=total_games+1, total_wins=total_wins+1, "
                "win_streak=win_streak+1, total_wagered=total_wagered+? WHERE user_id=?",
                (bet, gain, bp, bet, uu.user_id))
            await DB.execute("UPDATE users SET best_win_streak=MAX(best_win_streak,win_streak) WHERE user_id=?",
                             (uu.user_id,))
            await _touch_tx(uu.user_id, "game_win", gain, f"Ladder x{m}")
    u = await _read(user.user_id)
    update_quests(u, "play_games", 1); update_quests(u, "win_games", 1)
    update_quests(u, "bet_1000", bet); refresh_streak_quest(u)
    ach = check_ach(u)
    if ach: await award_ach(u.user_id, ach)
    await persist_extra(u)
    await state.clear()
    ach_line = ("\n\n🏅 " + ", ".join(ach)) if ach else ""
    await edit(call.message, f"💰 <b>Забрал на {step}</b> x{m}\n+{payout-bet} CF{ach_line}\n\n"
                              f"Баланс: <b>{u.balance}</b>", kb_main())
    await call.answer("Забрал!")

# ============================== DOUBLE / JACKPOT ==============================
async def start_double(call: CallbackQuery, state: FSMContext, user: User):
    await state.set_state(St.double)
    if call.message is not None:
        mention = umention(user.user_id, user.username)
        await edit(call.message, f"2️⃣ <b>Дабл</b> — {mention}\n50/50 удвой. Введи ставку:", kb_back())
    await call.answer()

@dp.message(St.double)
async def bet_double(msg: Message, state: FSMContext, user: User):
    if msg.text is None: return
    bet, err = parse_bet(msg.text, user)
    if err or bet is None:
        if msg.chat.type == "private": await msg.answer(err or "❌")
        return
    await state.update_data(bet=bet, owner=user.user_id)
    await msg.answer(f"2️⃣ Ставка <b>{bet} CF</b>",
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                         [InlineKeyboardButton(text="🔥 Удвоить!", callback_data="double:go")],
                         [InlineKeyboardButton(text="🚫 Отмена", callback_data="menu:main")]]))

@dp.callback_query(F.data == "double:go")
async def double_go(call: CallbackQuery, state: FSMContext, user: User):
    if call.message is None: await call.answer(); return
    data = await state.get_data()
    bet = data.get("bet"); owner = data.get("owner")
    if owner is not None and owner != user.user_id:
        await call.answer("Не твоя.", show_alert=True); return
    if not bet: await call.answer("Сессия.", show_alert=True); return
    win = RNG.random() < 0.5
    if win:
        async with ulock(user.user_id):
            async with tx():
                uu = await _read(user.user_id)
                if uu is None or uu.balance < bet: return
                bp = min(uu.bonus_balance, bet)
                await DB.execute(
                    "UPDATE users SET balance=balance-?+?, bonus_balance=bonus_balance-?, "
                    "total_games=total_games+1, total_wins=total_wins+1, "
                    "win_streak=win_streak+1, total_wagered=total_wagered+? WHERE user_id=?",
                    (bet, bet*2, bp, bet, uu.user_id))
                await DB.execute("UPDATE users SET best_win_streak=MAX(best_win_streak,win_streak) WHERE user_id=?",
                                 (uu.user_id,))
                await _touch_tx(uu.user_id, "game_win", bet, "Double x2")
    else:
        cb = await apply_loss(user.user_id, bet, "game_lose", "Double")
        if cb < 0: await call.answer("Недостаточно.", show_alert=True); return
    u = await _read(user.user_id)
    update_quests(u, "play_games", 1)
    if win: update_quests(u, "win_games", 1)
    else: reset_streak_quest(u)
    update_quests(u, "bet_1000", bet)
    if win: refresh_streak_quest(u)
    ach = check_ach(u)
    if ach: await award_ach(u.user_id, ach)
    await persist_extra(u); await state.clear()
    ach_line = ("\n\n🏅 " + ", ".join(ach)) if ach else ""
    txt = (f"2️⃣ <b>Удвоил!</b> +{bet} CF{ach_line}\nБаланс: <b>{u.balance}</b>" if win
           else f"2️⃣ <b>Проиграл!</b> -{bet} CF{ach_line}\nБаланс: <b>{u.balance}</b>")
    await edit(call.message, txt, kb_main())
    await call.answer("Удвой!" if win else "Увы...")

JACKPOT_CHANCE, JACKPOT_MULT = 0.01, 100

async def start_jackpot(call: CallbackQuery, state: FSMContext, user: User):
    await state.set_state(St.jackpot)
    if call.message is not None:
        mention = umention(user.user_id, user.username)
        await edit(call.message, f"💎 <b>Джекпот</b> — {mention}\nШанс x100 = 1%\nВведи ставку:", kb_back())
    await call.answer()

@dp.message(St.jackpot)
async def bet_jackpot(msg: Message, state: FSMContext, user: User):
    if msg.text is None: return
    bet, err = parse_bet(msg.text, user)
    if err or bet is None:
        if msg.chat.type == "private": await msg.answer(err or "❌")
        return
    await state.update_data(bet=bet, owner=user.user_id)
    await msg.answer(f"💎 Ставка <b>{bet} CF</b>",
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                         [InlineKeyboardButton(text="💎 Испытать", callback_data="jp:go")],
                         [InlineKeyboardButton(text="🚫 Отмена", callback_data="menu:main")]]))

@dp.callback_query(F.data == "jp:go")
async def jackpot_go(call: CallbackQuery, state: FSMContext, user: User):
    if call.message is None: await call.answer(); return
    data = await state.get_data()
    bet = data.get("bet"); owner = data.get("owner")
    if owner is not None and owner != user.user_id:
        await call.answer("Не твоя.", show_alert=True); return
    if not bet: await call.answer("Сессия.", show_alert=True); return
    win = RNG.random() < JACKPOT_CHANCE
    if win:
        gain = bet * (JACKPOT_MULT - 1)
        async with ulock(user.user_id):
            async with tx():
                uu = await _read(user.user_id)
                if uu is None or uu.balance < bet: return
                bp = min(uu.bonus_balance, bet)
                await DB.execute(
                    "UPDATE users SET balance=balance-?+?, bonus_balance=bonus_balance-?, "
                    "total_games=total_games+1, total_wins=total_wins+1, "
                    "win_streak=win_streak+1, total_wagered=total_wagered+? WHERE user_id=?",
                    (bet, gain, bp, bet, uu.user_id))
                await DB.execute("UPDATE users SET best_win_streak=MAX(best_win_streak,win_streak) WHERE user_id=?",
                                 (uu.user_id,))
                await _touch_tx(uu.user_id, "game_win", gain, "Jackpot")
    else:
        cb = await apply_loss(user.user_id, bet, "game_lose", "Jackpot")
        if cb < 0: await call.answer("Недостаточно.", show_alert=True); return
    u = await _read(user.user_id)
    update_quests(u, "play_games", 1)
    if win: update_quests(u, "win_games", 1)
    else: reset_streak_quest(u)
    update_quests(u, "bet_1000", bet)
    if win: refresh_streak_quest(u)
    ach = check_ach(u)
    if ach: await award_ach(u.user_id, ach)
    await persist_extra(u); await state.clear()
    ach_line = ("\n\n🏅 " + ", ".join(ach)) if ach else ""
    txt = (f"💎💎💎 <b>ДЖЕКПОТ!</b>\n+{bet*(JACKPOT_MULT-1)} CF{ach_line}\nБаланс: <b>{u.balance}</b>" if win
           else f"💀 <b>Мимо!</b>\n-{bet} CF{ach_line}\nБаланс: <b>{u.balance}</b>")
    await edit(call.message, txt, kb_main())
    await call.answer("ДЖЕКПОТ!!!" if win else "Не повезло")

# ============================== WITHDRAW ==============================
@dp.callback_query(F.data == "menu:withdraw")
async def cb_withdraw(call: CallbackQuery, state: FSMContext, user: User):
    if call.message is None: await call.answer(); return
    if user.paid_balance < MIN_WITHDRAW_CF:
        await call.answer(f"⚠️ К выводу {user.paid_balance}. Минимум {MIN_WITHDRAW_CF}.", show_alert=True); return
    await state.set_state(St.wd_dest)
    await state.update_data(wd_method=None)
    txt = (f"💸 <b>Вывод</b>\n\n🪙 Общий: <b>{user.balance}</b>\n🎁 Бонусных: <b>{user.bonus_balance}</b>\n"
           f"✅ К выводу: <b>{user.paid_balance}</b> ({fmt_rub(user.paid_balance)})\n"
           f"📉 Минимум: {MIN_WITHDRAW_CF}\n\n⚠️ Бонусные нельзя.\n\nВыбери способ:")
    await edit(call.message, txt, kb_withdraw()); await call.answer()

@dp.callback_query(F.data.startswith("wd:"))
async def wd_method(call: CallbackQuery, state: FSMContext):
    if call.message is None: await call.answer(); return
    m = call.data.split(":",1)[1]
    if m not in ("ton","mir"): await call.answer(); return
    await state.update_data(wd_method=m)
    await state.set_state(St.wd_dest)
    txt = "💎 Введи адрес Keeper:" if m == "ton" else "💳 Введи номер карты МИР (16 цифр):"
    await edit(call.message, txt, kb_back()); await call.answer()

@dp.message(St.wd_dest)
async def wd_dest(msg: Message, state: FSMContext, user: User):
    if msg.text is None: return
    data = await state.get_data()
    m = data.get("wd_method")
    if m not in ("ton","mir"): await msg.answer("❌ Сессия."); await state.clear(); return
    v = msg.text.strip().replace(" ", "")
    if m == "ton" and not is_ton(v): await msg.answer("❌ Некорректный TON."); return
    if m == "mir" and not is_mir(v): await msg.answer("❌ Некорректная карта МИР."); return
    await state.update_data(wd_dest=v)
    await state.set_state(St.wd_amt)
    u = await _read(user.user_id)
    await msg.answer(f"✅ Принято. К выводу: <b>{u.paid_balance}</b>\nВведи сумму:")

@dp.message(St.wd_amt)
async def wd_amt(msg: Message, state: FSMContext, user: User):
    if msg.text is None: return
    try: amt = int(msg.text.strip())
    except ValueError: await msg.answer("❌ Целое."); return
    data = await state.get_data()
    m = data.get("wd_method"); dest = data.get("wd_dest")
    if m not in ("ton","mir") or not dest:
        await msg.answer("❌ Сессия."); await state.clear(); return
    if amt < MIN_WITHDRAW_CF: await msg.answer(f"❌ Минимум {MIN_WITHDRAW_CF}."); return
    if amt > MAX_WITHDRAW_CF: await msg.answer(f"❌ Максимум {MAX_WITHDRAW_CF}."); return
    async with ulock(user.user_id):
        async with tx():
            u = await _read(user.user_id)
            if u is None or amt > u.paid_balance:
                await msg.answer(f"❌ Только <b>{u.paid_balance if u else 0}</b> к выводу."); return
            rub = round(amt * COIN_RATE, 2)
            bp = min(u.bonus_balance, amt)
            await DB.execute(
                "UPDATE users SET balance=balance-?, bonus_balance=bonus_balance-? WHERE user_id=?",
                (amt, bp, u.user_id))
            await DB.execute(
                "INSERT INTO withdraws(user_id,username,amount_cf,amount_rub,method,destination,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (u.user_id, u.username, amt, rub, m, dest, time.time()))
            await _touch_tx(u.user_id, "withdraw", -amt, m)
            rid = (await (await DB.execute("SELECT last_insert_rowid() AS id")).fetchone())["id"]
    await state.clear()
    label = "TON (Keeper)" if m == "ton" else "Карта МИР"
    await msg.answer(f"⏳ Заявка <b>#{rid}</b>\n{amt} CF = {rub:.2f} ₽\n{label}\n<code>{esc(dest)}</code>",
                     reply_markup=kb_main())
    for aid in ADMIN_IDS:
        with suppress(TelegramAPIError):
            await bot.send_message(aid, f"💸 <b>Заявка #{rid}</b>\n👤 {udisp(u)} (ID <code>{u.user_id}</code>)\n"
                                          f"🪙 {amt} | 💵 {rub:.2f}\n💼 {label}\n📍 <code>{esc(dest)}</code>\n\n"
                                          f"<code>/approve {rid} &lt;tx&gt;</code>\n<code>/reject {rid}</code>")

# ============================== DEPOSIT ==============================
@dp.callback_query(F.data == "menu:deposit")
async def cb_deposit(call: CallbackQuery, user: User):
    if call.message is None: await call.answer(); return
    txt = (f"📥 <b>Пополнение</b>\n\n"
           f"💵 Минимум: {MIN_DEPOSIT_RUB} ₽\n\n"
           f"⚠️ <b>Оплата строго на мой аккаунт</b> {esc(DEPOSIT_CONTACT)}\n\n"
           f"Способы:\n"
           f"⭐ Telegram Stars\n"
           f"💳 Карта / Крипта\n"
           f"🎨 NFT (TON)\n\n"
           f"После оплаты напиши мне {esc(DEPOSIT_CONTACT)} и укажи свой ID:\n"
           f"<code>{user.user_id}</code>")
    await edit(call.message, txt, kb_deposit()); await call.answer()

@dp.callback_query(F.data == "dep:stars")
async def dep_stars(call: CallbackQuery, user: User):
    if call.message is None: await call.answer(); return
    txt = (
        f"⭐ <b>Пополнение через Telegram Stars</b>\n\n"
        f"⚠️ <b>Отправь звёзды строго на мой аккаунт</b> {esc(DEPOSIT_CONTACT)}\n\n"
        f"<b>Как это сделать:</b>\n"
        f"1. Открой мой профиль {esc(DEPOSIT_CONTACT)}\n"
        f"2. Отправь мне подарок или звёзды на нужную сумму\n"
        f"3. Напиши мне, что именно покупаешь:\n"
        f"   • VIP — <b>{VIP_PRICE_STARS} ⭐</b>\n"
        f"   • Или пополнение баланса\n"
        f"4. Укажи свой ID: <code>{user.user_id}</code>\n\n"
        f"Я проверю и зачислю вручную.\n\n"
        f"💠 VIP на 7 дней — <b>{VIP_PRICE_STARS} ⭐</b>"
    )
    await edit(call.message, txt, kb_back()); await call.answer()

@dp.callback_query(F.data == "dep:nft")
async def dep_nft(call: CallbackQuery, user: User):
    if call.message is None: await call.answer(); return
    txt = (f"🎨 <b>NFT</b>\n\n"
           f"⚠️ <b>Оплата строго на мой аккаунт</b> {esc(DEPOSIT_CONTACT)}\n\n"
           f"💎 <b>Через блокчейн TON</b>\nКошелёк: <code>{esc(NFT_WALLET or '—')}</code>\n"
           f"В комментарии к транзакции укажи свой ID.\n\n"
           f"👤 <b>Через аккаунт Telegram</b>\nПередай подарок мне на {esc(DEPOSIT_CONTACT)}.")
    await edit(call.message, txt, kb_nft()); await call.answer()

@dp.callback_query(F.data == "nft:chain")
async def nft_chain(call: CallbackQuery, user: User):
    if call.message is None: await call.answer(); return
    if not NFT_WALLET: await call.answer("NFT временно недоступен.", show_alert=True); return
    async with tx():
        await DB.execute("INSERT INTO nft_requests(user_id,method,address_or_link,note,created_at) "
                         "VALUES(?,?,?,?,?)", (user.user_id, "chain", NFT_WALLET, "", time.time()))
        rid = (await (await DB.execute("SELECT last_insert_rowid() AS id")).fetchone())["id"]
    txt = (f"💎 <b>NFT через блокчейн TON</b>\n\n"
           f"⚠️ <b>Оплата строго на мой аккаунт</b> {esc(DEPOSIT_CONTACT)}\n\n"
           f"Кошелёк: <code>{esc(NFT_WALLET)}</code>\n\n"
           f"В комментарии к транзакции укажи ID <code>{user.user_id}</code>.\n\n"
           f"Заявка <b>#{rid}</b> создана. Я свяжусь с тобой.")
    await edit(call.message, txt, kb_back())
    for aid in ADMIN_IDS:
        with suppress(TelegramAPIError):
            await bot.send_message(aid, f"🎨 <b>NFT заявка #{rid}</b>\n👤 {udisp(user)}")
    await call.answer("Заявка создана!")

@dp.callback_query(F.data == "nft:account")
async def nft_account(call: CallbackQuery, user: User):
    if call.message is None: await call.answer(); return
    txt = (f"👤 <b>NFT через аккаунт</b>\n\n"
           f"⚠️ <b>Оплата строго на мой аккаунт</b> {esc(DEPOSIT_CONTACT)}\n\n"
           f"Передай подарок мне на аккаунт {esc(DEPOSIT_CONTACT)}\n"
           f"После передачи напиши мне с ID <code>{user.user_id}</code>.")
    await edit(call.message, txt, kb_back()); await call.answer()

@dp.callback_query(F.data == "dep:manual")
async def dep_manual(call: CallbackQuery, user: User):
    if call.message is None: await call.answer(); return
    txt = (f"💳 <b>Карта / Крипта</b>\n\n"
           f"⚠️ <b>Оплата строго на мой аккаунт</b> {esc(DEPOSIT_CONTACT)}\n\n"
           f"💵 Минимум: {MIN_DEPOSIT_RUB} ₽\n\n"
           f"<b>Что делать:</b>\n"
           f"1. Напиши мне {esc(DEPOSIT_CONTACT)}\n"
           f"2. Укажи сумму\n"
           f"3. Оплати удобным способом\n"
           f"4. Сообщи свой ID: <code>{user.user_id}</code>\n\n"
           f"После оплаты я зачислю CF на баланс.")
    await edit(call.message, txt, kb_back()); await call.answer()

# ============================== ADMIN ==============================
@dp.message(Command("admin"))
async def cmd_admin(msg: Message, state: FSMContext, user: User):
    if user.user_id not in ADMIN_IDS: return
    await state.clear()
    await msg.answer("🛠 <b>Админ-панель</b>", reply_markup=kb_admin())

@dp.callback_query(F.data == "admin:panel")
async def cb_admin(call: CallbackQuery, state: FSMContext, user: User):
    if user.user_id not in ADMIN_IDS: await call.answer("Нет.", show_alert=True); return
    if call.message is None: await call.answer(); return
    await state.clear()
    await edit(call.message, "🛠 <b>Админ-панель</b>", kb_admin()); await call.answer()

@dp.callback_query(F.data == "admin:stats")
async def cb_admin_stats(call: CallbackQuery, user: User):
    if user.user_id not in ADMIN_IDS: await call.answer("Нет.", show_alert=True); return
    if call.message is None: await call.answer(); return
    async with DB.execute("SELECT COUNT(*) AS c, COALESCE(SUM(balance),0) AS b, "
                          "COALESCE(SUM(bonus_balance),0) AS bb, COALESCE(SUM(total_games),0) AS g, "
                          "COALESCE(SUM(total_wagered),0) AS w, COALESCE(SUM(total_deposits_cf),0) AS d, "
                          "COALESCE(SUM(total_withdraws_cf),0) AS wd FROM users") as cur:
        r = await cur.fetchone()
    async with DB.execute("SELECT COUNT(*) AS c FROM withdraws WHERE status='pending'") as cur:
        p = await cur.fetchone()
    txt = (f"📊 <b>Статистика</b>\n\n👥 Юзеров: <b>{r['c']}</b>\n"
           f"🪙 CF: <b>{fmt_big(r['b'])}</b>\n🎁 Бонусных: <b>{fmt_big(r['bb'])}</b>\n"
           f"🎮 Игр: <b>{r['g']}</b>\n💵 Оборот: <b>{fmt_big(r['w'])}</b>\n"
           f"📥 Депозитов: <b>{r['d']}</b>\n📤 Выводов: <b>{r['wd']}</b>\n⏳ Заявок: <b>{p['c']}</b>")
    await edit(call.message, txt, kb_admin_back()); await call.answer()

async def _resolve(raw: str) -> Optional[User]:
    raw = raw.strip()
    if raw.lstrip("-").isdigit(): return await _read(int(raw))
    if raw.startswith("@"): return await find_user_by_username(raw)
    return None

@dp.callback_query(F.data.startswith("admin:"))
async def admin_dispatch(call: CallbackQuery, state: FSMContext, user: User):
    if user.user_id not in ADMIN_IDS: await call.answer("Нет.", show_alert=True); return
    if call.message is None: await call.answer(); return
    action = call.data.split(":",1)[1]
    prompts = {
        "give": ("➕ <b>Выдать CF</b>\n\nID или @username:", St.a_give_t),
        "take": ("➖ <b>Забрать CF</b>\n\nID или @username:", St.a_take_t),
        "ban": ("🚫 <b>Бан</b>\n\nID или @username:", St.a_ban),
        "unban": ("✅ <b>Разбан</b>\n\nID или @username:", St.a_unban),
        "promo_new": ("🎟 <b>Промокод</b>\n\nСколько CF?", St.a_promo_a),
        "broadcast": ("📢 <b>Рассылка</b>\n\nТекст (без разметки):", St.a_cast),
        "vip": ("🏅 <b>VIP</b>\n\nID или @username:", St.a_vip_t),
    }
    if action in prompts:
        txt, st = prompts[action]
        await state.set_state(st); await edit(call.message, txt, kb_admin_back()); await call.answer(); return
    if action == "tournament":
        async with glock("tournaments"):
            tours = await kv_get("tournaments", [])
            if any(not t.get("finished") and t["ends_at"] > time.time() for t in tours):
                await call.answer("Уже идёт.", show_alert=True); return
            for t in tours: t["finished"] = True
            tid = await next_id("tournament")
            tours.append({"tournament_id": tid, "started_at": time.time(),
                          "ends_at": time.time() + TOURNAMENT_DURATION, "finished": False})
            await kv_set("tournaments", tours)
        await DB.execute("UPDATE users SET tournament_score=0 WHERE banned=0")
        await DB.commit()
        await edit(call.message, f"🏆 Турнир #{tid} запущен", kb_admin()); await call.answer(); return
    await call.answer("Неизвестно.", show_alert=True)

@dp.message(St.a_give_t)
async def a_give_t(msg: Message, state: FSMContext, user: User):
    if user.user_id not in ADMIN_IDS or msg.text is None: return
    t = await _resolve(msg.text)
    if not t: await msg.answer("❌ Не найден."); return
    await state.update_data(tid=t.user_id); await state.set_state(St.a_give_a)
    await msg.answer(f"👤 {udisp(t)}\nБаланс: {t.balance}\n\nСколько?")

@dp.message(St.a_give_a)
async def a_give_a(msg: Message, state: FSMContext, user: User):
    if user.user_id not in ADMIN_IDS or msg.text is None: return
    try: amt = int(msg.text.strip())
    except ValueError: await msg.answer("❌ Целое."); return
    if amt <= 0: await msg.answer("❌ Положительное."); return
    data = await state.get_data(); tid = data.get("tid")
    async with tx():
        await _update_balance(tid, amt, amt)
        await _touch_tx(tid, "admin_give", amt, "От админа")
    t = await _read(tid); ach = check_ach(t)
    if ach: await award_ach(tid, ach)
    await state.clear()
    await msg.answer(f"✅ +{amt} CF → {udisp(t)}", reply_markup=kb_admin())
    with suppress(TelegramAPIError): await bot.send_message(tid, f"🎁 +{amt} CF")

@dp.message(St.a_take_t)
async def a_take_t(msg: Message, state: FSMContext, user: User):
    if user.user_id not in ADMIN_IDS or msg.text is None: return
    t = await _resolve(msg.text)
    if not t: await msg.answer("❌ Не найден."); return
    await state.update_data(tid=t.user_id); await state.set_state(St.a_take_a)
    await msg.answer(f"👤 {udisp(t)}\nБаланс: {t.balance}\n\nСколько?")

@dp.message(St.a_take_a)
async def a_take_a(msg: Message, state: FSMContext, user: User):
    if user.user_id not in ADMIN_IDS or msg.text is None: return
    try: amt = int(msg.text.strip())
    except ValueError: await msg.answer("❌ Целое."); return
    if amt <= 0: await msg.answer("❌ Положительное."); return
    data = await state.get_data(); tid = data.get("tid")
    async with ulock(tid):
        t = await _read(tid)
        if not t or amt > t.balance: await msg.answer("❌ Мало."); return
        bp = min(t.bonus_balance, amt)
        async with tx():
            await DB.execute("UPDATE users SET balance=balance-?, bonus_balance=bonus_balance-? WHERE user_id=?",
                             (amt, bp, tid))
            await _touch_tx(tid, "admin_take", -amt, "Забрал админ")
    await state.clear()
    await msg.answer(f"✅ -{amt} CF у {udisp(t)}", reply_markup=kb_admin())

@dp.message(St.a_ban)
async def a_ban(msg: Message, state: FSMContext, user: User):
    if user.user_id not in ADMIN_IDS or msg.text is None: return
    t = await _resolve(msg.text)
    if not t: await msg.answer("❌ Не найден."); return
    await DB.execute("UPDATE users SET banned=1 WHERE user_id=?", (t.user_id,)); await DB.commit()
    await state.clear()
    await msg.answer(f"🚫 {udisp(t)}", reply_markup=kb_admin())

@dp.message(St.a_unban)
async def a_unban(msg: Message, state: FSMContext, user: User):
    if user.user_id not in ADMIN_IDS or msg.text is None: return
    t = await _resolve(msg.text)
    if not t: await msg.answer("❌ Не найден."); return
    await DB.execute("UPDATE users SET banned=0 WHERE user_id=?", (t.user_id,)); await DB.commit()
    await state.clear()
    await msg.answer(f"✅ {udisp(t)}", reply_markup=kb_admin())

@dp.message(St.a_promo_a)
async def a_promo_a(msg: Message, state: FSMContext, user: User):
    if user.user_id not in ADMIN_IDS or msg.text is None: return
    try: amt = int(msg.text.strip())
    except ValueError: await msg.answer("❌ Целое."); return
    if amt <= 0: await msg.answer("❌ Положительное."); return
    await state.update_data(promo_a=amt); await state.set_state(St.a_promo_u)
    await msg.answer("Сколько использований?")

@dp.message(St.a_promo_u)
async def a_promo_u(msg: Message, state: FSMContext, user: User):
    if user.user_id not in ADMIN_IDS or msg.text is None: return
    try: uses = int(msg.text.strip())
    except ValueError: await msg.answer("❌ Целое."); return
    if uses <= 0: await msg.answer("❌ Положительное."); return
    data = await state.get_data(); amt = data.get("promo_a", 0)
    code = gen_promo()
    async with glock("promos"):
        promos = await kv_get("promos", {})
        promos[code] = {"code": code, "reward": amt, "uses_left": uses,
                        "bonus_only": True, "created_at": time.time()}
        await kv_set("promos", promos)
    await state.clear()
    await msg.answer(f"🎟 Код: <code>{code}</code>\n{amt} CF × {uses}\n\n<code>/promo {code}</code>",
                     reply_markup=kb_admin())

@dp.message(St.a_cast)
async def a_cast(msg: Message, state: FSMContext, user: User):
    if user.user_id not in ADMIN_IDS or msg.text is None: return
    raw = msg.text
    await state.clear()
    sent = failed = 0
    async with DB.execute("SELECT user_id FROM users WHERE banned=0") as cur:
        rows = await cur.fetchall()
    for r in rows:
        try:
            await bot.send_message(r["user_id"], f"📢 {raw}", parse_mode=None)
            sent += 1
        except TelegramAPIError: failed += 1
        await asyncio.sleep(0.05)
    await msg.answer(f"✅ {sent} / ❌ {failed}", reply_markup=kb_admin())

@dp.message(St.a_vip_t)
async def a_vip_t(msg: Message, state: FSMContext, user: User):
    if user.user_id not in ADMIN_IDS or msg.text is None: return
    t = await _resolve(msg.text)
    if not t: await msg.answer("❌ Не найден."); return
    await state.update_data(tid=t.user_id); await state.set_state(St.a_vip_d)
    await msg.answer(f"👤 {udisp(t)}\nСколько дней VIP?")

@dp.message(St.a_vip_d)
async def a_vip_d(msg: Message, state: FSMContext, user: User):
    if user.user_id not in ADMIN_IDS or msg.text is None: return
    try: days = int(msg.text.strip())
    except ValueError: await msg.answer("❌ Целое."); return
    if days <= 0 or days > 365: await msg.answer("❌ 1–365."); return
    data = await state.get_data(); tid = data.get("tid")
    async with tx():
        t = await _read(tid)
        base = max(t.vip_until, time.time()) if t else time.time()
        await DB.execute("UPDATE users SET vip_until=? WHERE user_id=?", (base + days*86400, tid))
        await _touch_tx(tid, "vip", 0, f"VIP {days} дн.")
    t = await _read(tid); ach = check_ach(t)
    if ach: await award_ach(tid, ach)
    await state.clear()
    await msg.answer(f"🏅 VIP {days} дн → {udisp(t)}", reply_markup=kb_admin())

@dp.message(Command("approve"))
async def cmd_approve(msg: Message, user: User):
    if user.user_id not in ADMIN_IDS: return
    args = (msg.text or "").split()
    if len(args) < 2 or not args[1].isdigit(): await msg.answer("<code>/approve &lt;id&gt; [tx]</code>"); return
    rid = int(args[1]); tx_hash = args[2] if len(args) > 2 else ""
    async with glock("withdraw"):
        async with tx():
            cur = await DB.execute(
                "UPDATE withdraws SET status='approved', tx_hash=?, processed_by=?, processed_at=? "
                "WHERE request_id=? AND status='pending'",
                (tx_hash, user.user_id, time.time(), rid))
            if cur.rowcount != 1:
                await msg.answer("Заявка не найдена."); return
            async with DB.execute("SELECT user_id, amount_cf FROM withdraws WHERE request_id=?", (rid,)) as c:
                w = await c.fetchone()
            await DB.execute("UPDATE users SET total_withdraws_cf=total_withdraws_cf+? WHERE user_id=?",
                             (w["amount_cf"], w["user_id"]))
    await msg.answer(f"✅ #{rid}")
    with suppress(TelegramAPIError):
        await bot.send_message(w["user_id"], f"✅ Выплата #{rid}: {w['amount_cf']} CF")

@dp.message(Command("reject"))
async def cmd_reject(msg: Message, user: User):
    if user.user_id not in ADMIN_IDS: return
    args = (msg.text or "").split()
    if len(args) != 2 or not args[1].isdigit(): await msg.answer("<code>/reject &lt;id&gt;</code>"); return
    rid = int(args[1])
    async with glock("withdraw"):
        async with tx():
            cur = await DB.execute(
                "UPDATE withdraws SET status='rejected', processed_by=?, processed_at=? "
                "WHERE request_id=? AND status='pending'",
                (user.user_id, time.time(), rid))
            if cur.rowcount != 1: await msg.answer("Не найдена."); return
            async with DB.execute("SELECT user_id, amount_cf FROM withdraws WHERE request_id=?", (rid,)) as c:
                w = await c.fetchone()
            await _update_balance(w["user_id"], w["amount_cf"], 0)
            await _touch_tx(w["user_id"], "deposit", w["amount_cf"], "Возврат заявки")
    await msg.answer(f"❌ #{rid}")
    with suppress(TelegramAPIError):
        await bot.send_message(w["user_id"], f"❌ Заявка #{rid} отклонена.")

@dp.message(Command("deposit"))
async def cmd_deposit(msg: Message, user: User):
    if user.user_id not in ADMIN_IDS: return
    args = (msg.text or "").split()
    if len(args) != 3 or not args[1].isdigit() or not args[2].isdigit():
        await msg.answer("<code>/deposit &lt;id&gt; &lt;cf&gt;</code>"); return
    uid, amt = int(args[1]), int(args[2])
    if amt <= 0: await msg.answer("❌"); return
    async with tx():
        u = await _read(uid)
        if not u: await msg.answer("Нет юзера."); return
        await _update_balance(uid, amt, 0)
        await DB.execute("UPDATE users SET total_deposits_cf=total_deposits_cf+? WHERE user_id=?", (amt, uid))
        await _touch_tx(uid, "deposit", amt, "Ручной депозит")
    u = await _read(uid); ach = check_ach(u)
    if ach: await award_ach(uid, ach)
    await msg.answer(f"✅ +{amt} CF → {udisp(u)}")

# ============================== TOURNAMENT LOOP ==============================
async def tournaments_loop():
    while True:
        await asyncio.sleep(60)
        try:
            async with glock("tournaments"):
                tours = await kv_get("tournaments", [])
                to_finish = [t for t in tours if not t.get("finished") and t["ends_at"] <= time.time()]
                for t in to_finish: t["finished"] = True
                if to_finish: await kv_set("tournaments", tours)
            for t in to_finish:
                top = await top_users("tournament", 3, 1)
                rewards = [TOURNAMENT_REWARD_TOP1, TOURNAMENT_REWARD_TOP2, TOURNAMENT_REWARD_TOP3]
                for i, u_stub in enumerate(top):
                    try:
                        async with tx():
                            await _update_balance(u_stub.user_id, rewards[i], rewards[i])
                            await _touch_tx(u_stub.user_id, "tournament", rewards[i],
                                            f"Турнир #{t['tournament_id']} место {i+1}")
                        with suppress(TelegramAPIError):
                            await bot.send_message(u_stub.user_id,
                                f"🏆 Турнир #{t['tournament_id']} завершён! Место {i+1}. +{rewards[i]} CF")
                    except Exception as e:
                        log.exception("tournament reward uid=%s: %s", u_stub.user_id, e)
        except Exception as e:
            log.exception("tournaments_loop: %s", e)

@dp.callback_query(F.data == "menu:tournament")
async def cb_tournament(call: CallbackQuery):
    if call.message is None: await call.answer(); return
    async with glock("tournaments"):
        tours = await kv_get("tournaments", [])
    active = next((t for t in tours if not t.get("finished") and t["ends_at"] > time.time()), None)
    if not active:
        txt = (f"🏆 <b>Турнир</b>\n\nСейчас не идёт.\n"
               f"🥇 {TOURNAMENT_REWARD_TOP1} | 🥈 {TOURNAMENT_REWARD_TOP2} | 🥉 {TOURNAMENT_REWARD_TOP3}")
    else:
        rem = int(active["ends_at"] - time.time())
        top = await top_users("tournament", 5, 1)
        lines = [f"🏆 <b>Турнир #{active['tournament_id']}</b>\n",
                 f"⏰ До конца: <b>{fmt_cd(rem)}</b>\n",
                 f"🥇 {TOURNAMENT_REWARD_TOP1} | 🥈 {TOURNAMENT_REWARD_TOP2} | 🥉 {TOURNAMENT_REWARD_TOP3}\n"]
        if top:
            for i, u in enumerate(top, 1):
                lines.append(f"{i}. {udisp(u)} — <b>{fmt_big(u.tournament_score)}</b>")
        else: lines.append("Пока никто не играл.")
        txt = "\n".join(lines)
    await edit(call.message, txt, kb_back()); await call.answer()

# ============================== REFERRAL ==============================
@dp.callback_query(F.data == "menu:ref")
async def cb_ref(call: CallbackQuery, user: User):
    if call.message is None: await call.answer(); return
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{user.user_id}"
    txt = (f"👥 <b>Рефералы</b>\n\n🎁 Тебе: +{REFERRAL_BONUS_REFERRER} CF\n"
           f"🎁 Другу: +{REFERRAL_BONUS_NEW} CF\n\n"
           f"👥 Твоих: <b>{user.referral_count}</b>\n\n🔗 <code>{link}</code>")
    await edit(call.message, txt, kb_back()); await call.answer()

# ============================== START ==============================
async def set_commands():
    await bot.set_my_commands([
        BotCommand(command="start", description="Меню"),
        BotCommand(command="me", description="Профиль"),
        BotCommand(command="top", description="Топ"),
        BotCommand(command="promo", description="Промокод"),
        BotCommand(command="cancel", description="Отменить"),
        BotCommand(command="help", description="Справка"),
    ])

async def main():
    await bot.get_me()
    await db_init()
    await set_commands()
    asyncio.create_task(tournaments_loop())
    log.info("Бот запущен. Игр: %d. Курс: 1 CF = %.2f RUB", len(GAMES), COIN_RATE)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Остановлен.")