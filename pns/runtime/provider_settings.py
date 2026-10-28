# pns/runtime/provider_settings.py — 模型线路、计费账户与凭据的边界（CFG-1）
#
# 两条线路：**生成**（角色开口）和**判分**（Router 验收）。每条线路是一组
# { 账户, 协议, Base URL, 模型名, token }。线路挂在一个计费账户下，余额属于账户、
# token 属于线路。
#
# 面板存的是**卡片**（`settings/provider.json` v2）：每张卡片是一家配好的服务商，
# 可以存好几张；`active` 说生成用哪张、判分跟随生成还是用哪张。下游只看展开
# 出来的两条 `Line`。
#
#   * 文件不存在 —— 没有卡片、生效的是内建 `env` 卡片：两条线路的协议 / 地址 /
#     key 都来自 .env 那一套，模型名分别是 `GENERATOR_MODEL or MODEL` 与
#     `EVALUATOR_MODEL or MODEL`（与改动前逐字段一致）。key 照旧在用到时才从
#     进程环境取，不进快照。
#   * 文件存在但读不出、过不了校验、版本不认识（含 v1），这份配置整体不通过
#     （生产启动因此失败，fail-closed）。CFG-1 上线前没有 v1 的生产文件，不迁移。
#
# 账户目录 `settings/accounts.json` 是账户的唯一真相源；卡片只引用 id。
#
# 密钥边界：provider.json 是卡片凭据的唯一权威主文件（它的 .prev、写盘临时文件
# 和备份同样装着 token）。token 一律包在 `Secret` 里，`repr` / `str` 输出
# "<redacted>"、不能被 pickle；只有建 client 的地方 `.reveal()`、只有落盘时
# `Card.to_file()` 写出明文。对外的一切（API、reload 状态、审计）只走各对象的
# `public_dict()`，token 只给"有没有"和够长时的末 4 位。
import json
import os
import secrets
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterable, Mapping, Optional, Tuple
from urllib.parse import SplitResult, urlsplit

from pns.runtime import settings_files
from pns.runtime.settings_files import durable_write

PROVIDER_FILE = "provider.json"
ACCOUNTS_FILE = "accounts.json"
PROVIDER_VERSION = 2
ACCOUNTS_VERSION = 1

LINE_GENERATION = "generation"
LINE_JUDGE = "judge"
LINES = (LINE_GENERATION, LINE_JUDGE)

API_FORMATS = ("anthropic", "openai")

# 这条线路被用来判分时 thinking 怎么发（只对 anthropic 协议有意义）。判分是受约束
# 的分类调用，MiMo 会把整个输出预算花在思考块上，留不下 JSON。
#   auto     —— 旧行为：Base URL 是 MiMo 官方地址才关。env 派生的线路永远是它。
#   off      —— 总是发 thinking: disabled（经转发地址走 MiMo 时用这个）。
#   provider —— 什么都不发，由 provider 自己的默认决定。
JUDGE_THINKING_AUTO = "auto"
JUDGE_THINKING_OFF = "off"
JUDGE_THINKING_PROVIDER = "provider"
JUDGE_THINKING = (JUDGE_THINKING_AUTO, JUDGE_THINKING_OFF, JUDGE_THINKING_PROVIDER)

# `env` 是内建账户：代表 .env 那把 key 背后的计费账户。env 派生的两条线路都归它。
ENV_ACCOUNT = "env"
# `env` 也是内建卡片：由 .env 派生、只读，生效时账户固定是 env。
ENV_CARD = "env"
CARD_ID_PREFIX = "c-"
ACCOUNT_KINDS = (
    "env", "mimo", "deepseek", "novita", "openrouter", "stepfun", "siliconflow", "other",
)
BALANCE_MANUAL = "manual"
BALANCE_SOURCES = (BALANCE_MANUAL,)
CURRENCIES = ("CNY", "USD")

SOURCE_ENV = "env"
SOURCE_PANEL = "panel"

# 短于这个长度的 token 连末 4 位都不给：末 4 位在短 token 里占的比例太大。
HINT_MIN_LENGTH = 16
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


class ProviderSettingsError(ValueError):
    """线路 / 账户配置读不出来或过不了校验。

    消息是固定措辞加字段名，**从不**带字段的值：值里可能就是 token。
    """


class AccountInUse(ProviderSettingsError):
    """账户正被线路引用，不能退役。"""


# ── 密钥 ─────────────────────────────────────────────────────────────────


class Secret:
    """一个不肯被打印的字符串。

    `repr` / `str` / `format` 都是 "<redacted>"；没有 `__dict__`（`vars()` 拿不到
    值），也不能被 pickle 或 deepcopy 出去。要明文只有 `reveal()` 一条路，全仓库
    只有建 client 的地方调它。
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str):
            raise TypeError("Secret 只包字符串")
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __bool__(self) -> bool:
        return bool(self.reveal())

    def __repr__(self) -> str:
        return "<redacted>"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return "<redacted>"

    def __reduce__(self):
        raise TypeError("Secret 不能被序列化")

    def __deepcopy__(self, memo):
        return self

    def __copy__(self):
        return self

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and self.reveal() == other.reveal()

    # 不可哈希：哈希值由明文决定，没有任何地方需要拿 token 当键。
    __hash__ = None

    def hint(self) -> Optional[str]:
        """末 4 位；短 token 不给。"""
        value = self.reveal()
        return value[-4:] if len(value) >= HINT_MIN_LENGTH else None


class EnvSecret(Secret):
    """env 派生线路的 key：只记变量名，用到时才从进程环境取。

    跟改动前 `ModelSettings.api_key` 的语义一样 —— 构建快照时 key 不进内存，
    操作者改了环境变量、下一次建 client 就用新的。
    """

    __slots__ = ("key_name",)

    def __init__(self, key_name: str) -> None:
        self.key_name = key_name

    def reveal(self) -> str:
        return read_env_key(self.key_name)


def read_env_key(key_name: str) -> str:
    """env 派生线路取 key 的唯一入口（测试在这里替身）。"""
    return os.environ.get(key_name, "")


# ── 账户 ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Account:
    account_id: str
    name: str
    kind: str
    balance: str
    currency: str
    multiplier: str
    created_at: str
    retired_at: Optional[str] = None

    @property
    def retired(self) -> bool:
        return self.retired_at is not None

    def to_dict(self) -> Dict[str, Any]:
        """落盘形状（账户里没有密钥，落盘与对外投影同形）。"""
        payload = {
            "name": self.name,
            "kind": self.kind,
            "balance": self.balance,
            "currency": self.currency,
            "multiplier": self.multiplier,
            "created_at": self.created_at,
        }
        if self.retired_at is not None:
            payload["retired_at"] = self.retired_at
        return payload

    def public_dict(self) -> Dict[str, Any]:
        return {"id": self.account_id, **self.to_dict()}


def env_account(created_at: Optional[str] = None) -> Account:
    return Account(
        account_id=ENV_ACCOUNT,
        name=".env 凭据",
        kind="env",
        balance=BALANCE_MANUAL,
        currency="CNY",
        multiplier="1",
        created_at=_now() if created_at is None else created_at,
    )


def _parse_account(account_id: str, raw: Any) -> Account:
    label = f"账户 {account_id!r}"
    if not isinstance(raw, dict):
        raise ProviderSettingsError(f"{label} 必须是对象")
    fields = {}
    for key in ("name", "kind", "balance", "currency", "multiplier", "created_at"):
        value = raw.get(key)
        if not isinstance(value, str) or not value:
            raise ProviderSettingsError(f"{label} 缺少字段 {key}")
        fields[key] = value
    if fields["kind"] not in ACCOUNT_KINDS:
        raise ProviderSettingsError(f"{label} 的 kind 不认识")
    if fields["balance"] not in BALANCE_SOURCES:
        raise ProviderSettingsError(f"{label} 的余额来源不支持")
    if fields["currency"] not in CURRENCIES:
        raise ProviderSettingsError(f"{label} 的币种不支持")
    parse_multiplier(fields["multiplier"], label=label)
    retired_at = raw.get("retired_at")
    if retired_at is not None and not isinstance(retired_at, str):
        raise ProviderSettingsError(f"{label} 的 retired_at 不合法")
    return Account(account_id=account_id, retired_at=retired_at, **fields)


def parse_multiplier(raw: Any, *, label: str = "账户") -> Decimal:
    if not isinstance(raw, str):
        raise ProviderSettingsError(f"{label} 的倍率必须写成字符串")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise ProviderSettingsError(f"{label} 的倍率不是数字") from None
    if not value.is_finite() or value <= 0:
        raise ProviderSettingsError(f"{label} 的倍率必须是正数")
    return value


def validate_account_id(account_id: Any) -> str:
    if (
        not isinstance(account_id, str)
        or not 1 <= len(account_id) <= 64
        or not all(c.isascii() and (c.isalnum() or c in "-_") for c in account_id)
    ):
        raise ProviderSettingsError("账户 id 只能用 1–64 个字母、数字、- 和 _")
    return account_id


@dataclass(frozen=True)
class AccountCatalog:
    """账户目录：唯一真相源。`env` 账户不在文件里时由内建定义补上。"""

    accounts: Mapping[str, Account]

    def get(self, account_id: str) -> Optional[Account]:
        return self.accounts.get(account_id)

    def public_dict(self) -> Dict[str, Any]:
        return {aid: acc.public_dict() for aid, acc in sorted(self.accounts.items())}

    def to_file(self) -> Dict[str, Any]:
        return {
            "version": ACCOUNTS_VERSION,
            "accounts": {aid: acc.to_dict() for aid, acc in sorted(self.accounts.items())},
        }


def builtin_catalog() -> AccountCatalog:
    """只有内建 `env` 的目录（还没落过盘时的样子）。"""
    # created_at 留空串：这份内建定义不是一个已经存在过的身份，锚点不会认它（§4.3）。
    return AccountCatalog(accounts={ENV_ACCOUNT: env_account(created_at="")})


def load_accounts(settings_dir: Path) -> AccountCatalog:
    """读账户目录。文件不存在 = 只有内建 `env`；存在但坏了 = 配置不通过。"""
    path = Path(settings_dir) / ACCOUNTS_FILE
    raw = _read_json(path, label="账户目录")
    accounts: Dict[str, Account] = {}
    if raw is not None:
        if raw.get("version") != ACCOUNTS_VERSION:
            raise ProviderSettingsError("账户目录的版本不认识")
        entries = raw.get("accounts")
        if not isinstance(entries, dict):
            raise ProviderSettingsError("账户目录缺少 accounts")
        for account_id, entry in entries.items():
            validate_account_id(account_id)
            accounts[account_id] = _parse_account(account_id, entry)
    if ENV_ACCOUNT not in accounts:
        # 还没落盘：新代码第一次启动前，或开发环境。
        accounts[ENV_ACCOUNT] = builtin_catalog().get(ENV_ACCOUNT)
    return AccountCatalog(accounts=accounts)


def ensure_env_account(settings_dir: Path) -> Account:
    """`env` 账户落盘一次（幂等：已存在就原样返回，不改 created_at）。"""
    path = Path(settings_dir) / ACCOUNTS_FILE
    catalog = load_accounts(settings_dir)
    existing = catalog.get(ENV_ACCOUNT)
    if existing is not None and existing.created_at:
        return existing
    created = env_account()
    accounts = dict(catalog.accounts)
    accounts[ENV_ACCOUNT] = created
    durable_write(path, _dump(AccountCatalog(accounts=accounts).to_file()))
    return created


def _persisted(catalog: AccountCatalog) -> Dict[str, Account]:
    """要落盘的账户：内建但还没落过盘的 `env`（created_at 为空）不写进文件。"""
    return {aid: acc for aid, acc in catalog.accounts.items() if acc.created_at}


def _write_accounts(settings_dir: Path, accounts: Mapping[str, Account]) -> None:
    settings_files.durable_write(
        Path(settings_dir) / ACCOUNTS_FILE, _dump(AccountCatalog(accounts=accounts).to_file())
    )


def create_account(settings_dir: Path, account_id: Any, fields: Mapping[str, Any]) -> Account:
    """新建一个账户（§4.3）。id 一经创建不复用：退役的也算占用；`env` 是保留名。

    写盘按三档合同：NotPublished = 没建；PublishedNotDurable = 已经在文件里、
    耐久证实不了（身份照样不复用）。线路引用它之前，保存事务写 provider.txn
    时会刷同一个目录，账户目录项从那一刻起一定是耐久的。
    """
    catalog = load_accounts(settings_dir)
    account = new_account(catalog, account_id, fields)
    accounts = _persisted(catalog)
    accounts[account_id] = account
    _write_accounts(settings_dir, accounts)
    return account


def new_account(catalog: AccountCatalog, account_id: Any, fields: Mapping[str, Any]) -> Account:
    """校验一个要新建的账户，不写盘。id 不复用、`env` 是保留名。"""
    account_id = validate_account_id(account_id)
    if account_id == ENV_ACCOUNT:
        raise ProviderSettingsError("env 是保留的账户 id")
    if catalog.get(account_id) is not None:
        raise ProviderSettingsError("这个账户 id 已经用过了（含已退役的），换一个")
    raw = {key: fields.get(key) for key in ("name", "kind", "currency", "multiplier")}
    raw.update(balance=BALANCE_MANUAL, created_at=_now())
    account = _parse_account(account_id, raw)
    if account.kind == "env":
        raise ProviderSettingsError("kind 不能是 env")
    return account


def update_account(settings_dir: Path, account_id: str, fields: Mapping[str, Any]) -> Account:
    """改显示名或倍率。币种和 kind 不可改（要换就新建账户）；退役的不能改。"""
    catalog = load_accounts(settings_dir)
    current = catalog.get(account_id)
    if current is None or not current.created_at:
        raise KeyError(account_id)
    if current.retired:
        raise ProviderSettingsError("已退役的账户不能修改")
    for frozen in ("currency", "kind", "balance", "created_at", "retired_at"):
        if frozen in fields and fields[frozen] != getattr(current, frozen):
            raise ProviderSettingsError(f"账户的 {frozen} 不能修改；要换就新建一个账户")
    unknown = set(fields) - {"name", "multiplier", "currency", "kind", "balance", "created_at", "retired_at"}
    if unknown:
        raise ProviderSettingsError("请求里有不认识的字段")
    raw = current.to_dict()
    for key in ("name", "multiplier"):
        if key in fields:
            raw[key] = fields[key]
    updated = _parse_account(account_id, raw)
    accounts = _persisted(catalog)
    accounts[account_id] = updated
    _write_accounts(settings_dir, accounts)
    return updated


def retire_account(settings_dir: Path, account_id: str, in_use: Iterable[str]) -> Account:
    """标记退役（§4.3）。被有效线路引用的不许退役；历史账本行仍归它。"""
    catalog = load_accounts(settings_dir)
    current = catalog.get(account_id)
    if current is None or not current.created_at:
        raise KeyError(account_id)
    if current.retired:
        raise ProviderSettingsError("这个账户已经退役了")
    if account_id in set(in_use):
        raise AccountInUse("这个账户正被模型线路使用，先把线路换到别的账户再退役")
    retired = replace(current, retired_at=_now())
    accounts = _persisted(catalog)
    accounts[account_id] = retired
    _write_accounts(settings_dir, accounts)
    return retired


# ── 线路 ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Line:
    """一条解析好的线路（跟随已经展开成生成线路的副本）。"""

    name: str
    account: str
    api_format: str
    base_url: str
    model: str
    token: Secret
    # 写进判分 / 生成审计的 provider 标签。env 派生沿用 PROVIDER；面板线路用账户 kind。
    provider: str = ""
    judge_thinking: str = JUDGE_THINKING_AUTO
    # 这条线路出自哪张卡片（`env` = .env 派生）。跟随时判分线路记的是生成那张。
    card: str = ""

    @property
    def host(self) -> str:
        parts = _split_url(self.base_url)
        return (parts.hostname or "") if parts is not None else ""

    def public_dict(self) -> Dict[str, Any]:
        return {
            "card": self.card,
            "account": self.account,
            "api_format": self.api_format,
            "base_url": display_url(self.base_url),
            "model": self.model,
            "has_token": bool(self.token),
            "token_hint": self.token.hint(),
            "judge_thinking": self.judge_thinking,
        }


# ── 卡片（provider.json v2）──────────────────────────────────────────────
#
# 一张卡片 = 一家配好的服务商：显示名、计费账户、协议、Base URL、模型、token、
# 判分 thinking。`active` 说现在生成用哪张、判分跟随还是用哪张（可覆盖模型名）。
# `env` 是内建卡片：由 .env 派生、只读、不在文件里。文件不存在 ⇔ 没有卡片、
# 生效的是 env。
#
# 校验分两层：
#   * 形状（每张卡片都过，生效没生效都一样）：id、字段类型、显示名和模型非空、
#     协议、judge_thinking、账户在目录里、URL 能解析且有主机、只用 http(s)、
#     不夹用户名密码；
#   * 有效线路（只对生效的那一两张，§6）：https（本机例外）、token 非空、账户
#     没退役。
# 保存、回读、启动、reload、恢复都走 parse_document() + expand() 这一条路。


@dataclass(frozen=True)
class Card:
    card_id: str
    name: str
    account: str
    api_format: str
    base_url: str
    model: str
    token: Secret
    judge_thinking: str = JUDGE_THINKING_AUTO
    created_at: str = ""

    def to_file(self) -> Dict[str, Any]:
        """落盘形状：唯一会写出 token 明文的地方。"""
        payload = {
            "name": self.name,
            "account": self.account,
            "api_format": self.api_format,
            "base_url": self.base_url,
            "model": self.model,
            "token": self.token.reveal(),
            "judge_thinking": self.judge_thinking,
        }
        if self.created_at:
            payload["created_at"] = self.created_at
        return payload

    def public_dict(self) -> Dict[str, Any]:
        """对外投影：逐个字段列出，token 只给有没有和末 4 位。"""
        return {
            "id": self.card_id,
            "name": self.name,
            "account": self.account,
            "api_format": self.api_format,
            "base_url": display_url(self.base_url),
            "model": self.model,
            "has_token": bool(self.token),
            "token_hint": self.token.hint(),
            "judge_thinking": self.judge_thinking,
            "created_at": self.created_at,
            "builtin": False,
        }


@dataclass(frozen=True)
class JudgeChoice:
    """判分用什么：跟随生成，或者指定一张卡片（可覆盖模型名）。两者互斥。"""

    follow: bool
    card: Optional[str] = None
    model: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        if self.follow:
            return {"follow": LINE_GENERATION}
        payload: Dict[str, Any] = {"card": self.card}
        if self.model is not None:
            payload["model"] = self.model
        return payload


@dataclass(frozen=True)
class Active:
    generation: str
    judge: JudgeChoice

    def cards(self) -> Tuple[str, ...]:
        """实际被引用的卡片 id（含 env）。跟随时判分引用的就是生成那张。"""
        used = [self.generation]
        if not self.judge.follow and self.judge.card not in used:
            used.append(self.judge.card)
        return tuple(used)

    def to_dict(self) -> Dict[str, Any]:
        return {"generation": self.generation, "judge": self.judge.to_dict()}


DEFAULT_ACTIVE = Active(generation=ENV_CARD, judge=JudgeChoice(follow=True))


@dataclass(frozen=True)
class ProviderDocument:
    """provider.json 解析好的内容（只过了形状校验）。"""

    cards: Mapping[str, Card]
    active: Active
    # 删掉过的卡片 id：账本里可能还记着它们，新卡片永远不再用（卡片 id 不复用）。
    retired_card_ids: FrozenSet[str] = frozenset()

    @property
    def is_default(self) -> bool:
        """没有卡片、生效的是 env 且判分跟随、也没有要记住的退役 id —— 等于文件不存在。

        有退役 id 时文件留着一个没有卡片的壳：加载结果跟文件不存在一模一样，只是
        那些 id 还记着。
        """
        return not self.cards and self.active == DEFAULT_ACTIVE and not self.retired_card_ids

    def taken_ids(self) -> FrozenSet[str]:
        return frozenset(self.cards) | self.retired_card_ids

    def to_file(self, *, updated_by: str) -> Dict[str, Any]:
        payload = {
            "version": PROVIDER_VERSION,
            "cards": {cid: card.to_file() for cid, card in self.cards.items()},
            "active": self.active.to_dict(),
            "updated_at": _now(),
            "updated_by": updated_by,
        }
        if self.retired_card_ids:
            payload["retired_card_ids"] = sorted(self.retired_card_ids)
        return payload


EMPTY_DOCUMENT = ProviderDocument(cards={}, active=DEFAULT_ACTIVE)

# 解析不了或夹着凭据的地址在公开投影里一律显示成这个，不回显原文。
URL_PLACEHOLDER = "（地址无法显示）"


def display_url(url: str) -> str:
    parts = _split_url(url)
    if parts is None or parts.username or parts.password:
        return URL_PLACEHOLDER
    return url


def validate_card_id(card_id: Any) -> str:
    """卡片 id 规则同账户 id；`env` 是保留名。"""
    if (
        not isinstance(card_id, str)
        or not 1 <= len(card_id) <= 64
        or not all(c.isascii() and (c.isalnum() or c in "-_") for c in card_id)
    ):
        raise ProviderSettingsError("卡片 id 只能用 1–64 个字母、数字、- 和 _")
    if card_id == ENV_CARD:
        raise ProviderSettingsError("env 是内建卡片，不能用作卡片 id")
    return card_id


def new_card_id(taken: Iterable[str]) -> str:
    """服务端生成卡片 id：`c-` 加 8 位随机十六进制，避开已有的卡片和账户 id。"""
    taken = set(taken)
    while True:
        card_id = CARD_ID_PREFIX + secrets.token_hex(4)
        if card_id not in taken:
            return card_id


def _card_label(card_id: str) -> str:
    return f"卡片 {card_id}"


def _parse_card(card_id: str, raw: Any, catalog: AccountCatalog) -> Card:
    """一张卡片的形状校验（不看它生效没生效）。"""
    validate_card_id(card_id)
    label = _card_label(card_id)
    if not isinstance(raw, dict):
        raise ProviderSettingsError(f"{label} 必须是对象")
    fields = {}
    for key in ("name", "account", "api_format", "base_url", "model", "token"):
        value = raw.get(key)
        if not isinstance(value, str):
            raise ProviderSettingsError(f"{label} 缺少字段 {key}")
        fields[key] = value
    if not fields["name"].strip():
        raise ProviderSettingsError(f"{label} 的显示名不能为空")
    if not fields["model"].strip():
        raise ProviderSettingsError(f"{label} 的模型名不能为空")
    if fields["api_format"] not in API_FORMATS:
        raise ProviderSettingsError(f"{label} 的协议必须是 {'/'.join(API_FORMATS)} 之一")
    _check_url_shape(fields["base_url"], label)
    if catalog.get(fields["account"]) is None:
        raise ProviderSettingsError(f"{label} 引用的账户不存在")
    created_at = raw.get("created_at", "")
    if not isinstance(created_at, str):
        raise ProviderSettingsError(f"{label} 的 created_at 不合法")
    return Card(
        card_id=card_id,
        name=fields["name"],
        account=fields["account"],
        api_format=fields["api_format"],
        base_url=fields["base_url"],
        model=fields["model"],
        token=Secret(fields["token"]),
        judge_thinking=_judge_thinking(raw.get("judge_thinking"), label),
        created_at=created_at,
    )


def _check_url_shape(url: str, label: str) -> None:
    parts = _split_url(url)
    if parts is None or not parts.hostname or parts.scheme not in ("http", "https"):
        raise ProviderSettingsError(f"{label} 的 Base URL 不合法")
    if parts.username or parts.password:
        # URL 里夹凭据会原样出现在公开投影里。
        raise ProviderSettingsError(f"{label} 的 Base URL 不能带用户名或密码")


def _parse_active(raw: Any, cards: Mapping[str, Card]) -> Active:
    if not isinstance(raw, dict) or set(raw) != {"generation", "judge"}:
        raise ProviderSettingsError("active 必须是 { generation, judge }")
    generation = raw["generation"]
    if not isinstance(generation, str) or (generation != ENV_CARD and generation not in cards):
        raise ProviderSettingsError("生成用的卡片不存在")
    judge = raw["judge"]
    if not isinstance(judge, dict):
        raise ProviderSettingsError("判分设置必须是对象")
    if "follow" in judge:
        # 跟随时用生成卡片的全部字段，不许再带 card 或 model。
        if set(judge) != {"follow"} or judge["follow"] != LINE_GENERATION:
            raise ProviderSettingsError("判分跟随时只能写 { follow: generation }，不能再带卡片或模型")
        return Active(generation=generation, judge=JudgeChoice(follow=True))
    if not set(judge) <= {"card", "model"} or "card" not in judge:
        raise ProviderSettingsError("判分要么跟随生成，要么指定一张卡片")
    card = judge["card"]
    if not isinstance(card, str) or (card != ENV_CARD and card not in cards):
        raise ProviderSettingsError("判分用的卡片不存在")
    model = judge.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ProviderSettingsError("判分的覆盖模型名不能为空")
    return Active(generation=generation, judge=JudgeChoice(follow=False, card=card, model=model))


def parse_document(raw: Mapping[str, Any], catalog: AccountCatalog) -> ProviderDocument:
    """provider.json 的内容 → 卡片和 active（形状校验）。版本不认识一律拒绝。"""
    if raw.get("version") != PROVIDER_VERSION:
        raise ProviderSettingsError("线路配置的版本不认识")
    entries = raw.get("cards")
    if not isinstance(entries, dict):
        raise ProviderSettingsError("线路配置缺少 cards")
    cards = {card_id: _parse_card(card_id, entry, catalog) for card_id, entry in entries.items()}
    retired = raw.get("retired_card_ids", [])
    if not isinstance(retired, list):
        raise ProviderSettingsError("retired_card_ids 必须是列表")
    for card_id in retired:
        validate_card_id(card_id)
        if card_id in cards:
            raise ProviderSettingsError("退役的卡片 id 又出现在卡片里")
    return ProviderDocument(
        cards=cards, active=_parse_active(raw.get("active"), cards),
        retired_card_ids=frozenset(retired),
    )


def read_document(settings_dir: Path, catalog: AccountCatalog) -> Optional[ProviderDocument]:
    """读改写用：不存在 → None；存在但读不出或过不了形状校验 → 抛错。

    坏文件不能当成空目录接着写（会把里面的卡片连同 token 一起抹掉），要走
    明确的恢复流程。
    """
    raw = _read_json(Path(settings_dir) / PROVIDER_FILE, label="线路配置")
    return None if raw is None else parse_document(raw, catalog)


def env_settings(env: Mapping[str, str]) -> Tuple[str, str, str, str]:
    """env 那一套：(协议, Base URL, key 变量名, provider 标签)。校验同改动前。"""
    api_format = env.get("API_FORMAT", "anthropic")
    if api_format not in API_FORMATS:
        raise ProviderSettingsError(
            f"API_FORMAT 必须是 {'/'.join(API_FORMATS)} 之一"
        )
    base_url = env.get("BASE_URL", "https://api.xiaomimimo.com/anthropic")
    if not base_url:
        raise ProviderSettingsError("BASE_URL 不能为空")
    key_name = env.get("PNS_API_KEY_NAME", "MIMO_API_KEY")
    if not key_name:
        raise ProviderSettingsError("PNS_API_KEY_NAME 不能为空")
    return api_format, base_url, key_name, env.get("PROVIDER", "")


def _env_models(env: Mapping[str, str]) -> Tuple[str, str]:
    """env 卡片的 (生成模型, 判分模型)。"""
    model = env.get("MODEL", "mimo-v2.6-pro")
    return env.get("GENERATOR_MODEL") or model, env.get("EVALUATOR_MODEL") or model


def _env_line(name: str, env: Mapping[str, str], model: str) -> Line:
    api_format, base_url, key_name, provider = env_settings(env)
    return Line(
        name=name, account=ENV_ACCOUNT, api_format=api_format, base_url=base_url,
        model=model, token=EnvSecret(key_name), provider=provider, card=ENV_CARD,
    )


def env_lines(env: Mapping[str, str]) -> Tuple[Line, Line]:
    """文件不存在时的两条线路。判分模型取 EVALUATOR_MODEL（与改动前逐字段一致）。"""
    generation_model, judge_model = _env_models(env)
    return (
        _env_line(LINE_GENERATION, env, generation_model),
        _env_line(LINE_JUDGE, env, judge_model),
    )


def env_card_view(env: Mapping[str, str]) -> Dict[str, Any]:
    """内建 env 卡片的公开投影。env 本身坏了也照样给出来，只是标成不可用。"""
    view: Dict[str, Any] = {
        "id": ENV_CARD, "name": ".env", "account": ENV_ACCOUNT, "builtin": True,
    }
    try:
        api_format, base_url, key_name, _ = env_settings(env)
    except ProviderSettingsError as e:
        return {**view, "available": False, "problem": str(e)}
    generation_model, judge_model = _env_models(env)
    token = EnvSecret(key_name)
    return {
        **view,
        "available": True,
        "api_format": api_format,
        "base_url": display_url(base_url),
        "model": generation_model,
        "judge_model": judge_model,
        "has_token": bool(token),
        "token_hint": token.hint(),
        "judge_thinking": JUDGE_THINKING_AUTO,
    }


def env_is_complete(env: Mapping[str, str]) -> bool:
    """旧镜像在生产上能不能只靠这套 env 起来（它只检查协议合法和 key 非空）。"""
    try:
        _, _, key_name, _ = env_settings(env)
    except ProviderSettingsError:
        return False
    return bool(env.get(key_name))


def _card_line(name: str, card: Card, catalog: AccountCatalog, model: str) -> Line:
    account = catalog.get(card.account)
    return Line(
        name=name, account=card.account, api_format=card.api_format,
        base_url=card.base_url, model=model, token=card.token,
        provider=account.kind if account is not None else "",
        judge_thinking=card.judge_thinking, card=card.card_id,
    )


def expand(document: ProviderDocument, catalog: AccountCatalog, env: Mapping[str, str]) -> Tuple[Line, Line]:
    """生效的卡片 → 两条线路。判分模型的取法（审查 F3）：

      * 判分跟随、生成用普通卡片 → 生成卡片的模型；
      * 判分跟随、生成用 env      → EVALUATOR_MODEL or MODEL（特例：与文件不存在
        时一致，env 卡片本来就分开配生成和判分模型）；
      * 判分指定 env、不覆盖      → EVALUATOR_MODEL or MODEL；
      * 判分指定卡片、不覆盖      → 那张卡片的模型；
      * 指定了覆盖                → 覆盖的模型。

    只在引用了 env 时才读 env：没选 env 时坏掉的 env 不否决合法的卡片。
    """
    active = document.active

    def line_of(name: str, card_id: str, model: Optional[str]) -> Line:
        if card_id == ENV_CARD:
            generation_model, judge_model = _env_models(env)
            default = generation_model if name == LINE_GENERATION else judge_model
            return _env_line(name, env, model or default)
        card = document.cards[card_id]
        return _card_line(name, card, catalog, model or card.model)

    generation = line_of(LINE_GENERATION, active.generation, None)
    if active.judge.follow:
        judge = line_of(LINE_JUDGE, active.generation, None)
    else:
        judge = line_of(LINE_JUDGE, active.judge.card, active.judge.model)
    return generation, judge


def _judge_thinking(raw: Any, label: str) -> str:
    """可选字段，缺省是 auto。"""
    if raw is None:
        return JUDGE_THINKING_AUTO
    if raw not in JUDGE_THINKING:
        raise ProviderSettingsError(f"{label} 的 judge_thinking 只能是 {'/'.join(JUDGE_THINKING)}")
    return raw


def validate_line(line: Line, catalog: AccountCatalog, *, require_token: bool = True) -> None:
    """§6 有效线路校验（只对生效的线路）。启动、保存、恢复共用。"""
    label = "生成线路" if line.name == LINE_GENERATION else "判分线路"
    if line.api_format not in API_FORMATS:
        raise ProviderSettingsError(f"{label} 的协议必须是 {'/'.join(API_FORMATS)} 之一")
    parts = _split_url(line.base_url)
    if parts is None or not parts.hostname:
        raise ProviderSettingsError(f"{label} 的 Base URL 不合法")
    if parts.scheme != "https" and not (
        parts.scheme == "http" and parts.hostname in _LOCAL_HOSTS
    ):
        raise ProviderSettingsError(f"{label} 的 Base URL 必须是 https（本机地址例外）")
    if parts.username or parts.password:
        raise ProviderSettingsError(f"{label} 的 Base URL 不能带用户名或密码")
    if not line.model.strip():
        raise ProviderSettingsError(f"{label} 的模型名不能为空")
    if require_token and not line.token:
        raise ProviderSettingsError(f"{label} 没有可用的 token")
    account = catalog.get(line.account)
    if account is None:
        raise ProviderSettingsError(f"{label} 引用的账户不存在")
    if account.retired:
        raise ProviderSettingsError(f"{label} 引用的账户已经退役")


def validate_settings(settings: "ModelSettings", catalog: AccountCatalog) -> None:
    validate_line(settings.generation, catalog)
    validate_line(settings.judge, catalog)


def validate_for_publish(settings: "ModelSettings", catalog: AccountCatalog, env: Mapping[str, str]) -> None:
    """保存发布用的有效线路校验：跟构建读同一份环境（进程环境 + 磁盘 .env）。

    env 卡片的 key 按这份环境判断，不靠 EnvSecret 懒读进程环境 —— 构建时 .env
    还没落进进程环境，懒读看到的是旧值，会替一份新的、缺 key 的候选作证明。
    """
    for line in (settings.generation, settings.judge):
        if isinstance(line.token, EnvSecret):
            validate_line(line, catalog, require_token=False)
            if not env.get(line.token.key_name):
                label = "生成线路" if line.name == LINE_GENERATION else "判分线路"
                raise ProviderSettingsError(f"{label} 没有可用的 token")
        else:
            validate_line(line, catalog)


@dataclass(frozen=True)
class ModelSettings:
    """一份配置快照里的模型设定：两条解析好的线路 + 判分阈值，以及它们出自的卡片。"""

    generation: Line
    judge: Line
    source: str
    ooc_threshold: float
    # env 那一套凭据（协议 / 地址 / key / 模型）在当前进程环境里是否完整。
    # 面板凭据时这关系到"能不能直接回滚到旧镜像"（§9）。
    env_complete: bool = True
    # env 那把 key 的变量名（PNS_API_KEY_NAME）。env 派生线路从它取 key；面板
    # 线路不用它，但回滚预检要知道旧镜像会去找哪个变量。
    env_key_name: str = "MIMO_API_KEY"
    document: ProviderDocument = EMPTY_DOCUMENT
    # 内建 env 卡片的公开投影（构建时算好；里面没有 key，只有有没有和末 4 位）。
    env_card: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: Mapping[str, str], *, ooc_threshold: float = 5.0) -> "ModelSettings":
        """只看 env 的派生结果（等于面板从没存过时的设定）。"""
        generation, judge = env_lines(env)
        return cls(
            generation=generation, judge=judge, source=SOURCE_ENV,
            ooc_threshold=ooc_threshold, env_complete=env_is_complete(env),
            env_key_name=env_settings(env)[2], env_card=env_card_view(env),
        )

    @property
    def active(self) -> Active:
        return self.document.active

    @property
    def judge_follows(self) -> bool:
        return self.active.judge.follow

    @property
    def generator_model(self) -> str:
        return self.generation.model

    @property
    def evaluator_model(self) -> str:
        return self.judge.model

    def line(self, name: str) -> Line:
        if name == LINE_GENERATION:
            return self.generation
        if name == LINE_JUDGE:
            return self.judge
        raise KeyError(name)

    def public_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "cards": [card.public_dict() for card in _ordered(self.document.cards)],
            "env_card": dict(self.env_card),
            "active": self.active.to_dict(),
            "generation": self.generation.public_dict(),
            "judge": self.judge.public_dict(),
            "generator_model": self.generator_model,
            "evaluator_model": self.evaluator_model,
            "ooc_threshold": self.ooc_threshold,
            "env_complete": self.env_complete,
        }

    to_dict = public_dict


def _ordered(cards: Mapping[str, Card]) -> Iterable[Card]:
    return sorted(cards.values(), key=lambda card: (card.created_at, card.card_id))


def settings_from_document(
    document: Optional[ProviderDocument],
    catalog: AccountCatalog,
    env: Mapping[str, str],
    *,
    ooc_threshold: float,
) -> ModelSettings:
    """文件内容 → 校验过的模型设定。读文件和保存前校验候选共用。

    文件不存在（None）只做改动前那几项 env 检查 —— key 缺不缺由生产启动检查和
    建世界的地方分别把关，开发环境没有 key 也要能起来。文件存在时对生效的线路
    做完整的有效线路校验。
    """
    if document is None:
        return ModelSettings.from_env(env, ooc_threshold=ooc_threshold)
    if not document.cards and document.active == DEFAULT_ACTIVE:
        # 只记着退役 id 的壳：跟文件不存在一模一样。
        return replace(ModelSettings.from_env(env, ooc_threshold=ooc_threshold), document=document)
    generation, judge = expand(document, catalog, env)
    settings = ModelSettings(
        generation=generation, judge=judge, source=SOURCE_PANEL, ooc_threshold=ooc_threshold,
        # 旧 env 不完整只影响能不能回滚，不否决当前合法的卡片。
        env_complete=env_is_complete(env),
        env_key_name=env.get("PNS_API_KEY_NAME", "MIMO_API_KEY"),
        document=document,
        env_card=env_card_view(env),
    )
    for line in (generation, judge):
        # 出自 env 卡片的线路跟文件不存在时一样，只做 env_settings 那几项检查；
        # 卡片线路的 token 只可能来自这个文件，在这里就拦。
        if line.card != ENV_CARD:
            validate_line(line, catalog)
    return settings


def load_model_settings(
    settings_dir: Path, env: Mapping[str, str], *, ooc_threshold: float
) -> Tuple[ModelSettings, AccountCatalog]:
    """读出这份配置的模型设定。面板文件存在就以它为准，否则 env 派生。"""
    catalog = load_accounts(settings_dir)
    document = read_document(settings_dir, catalog)
    return settings_from_document(document, catalog, env, ooc_threshold=ooc_threshold), catalog


def candidate_payload(document: ProviderDocument, *, updated_by: str) -> Optional[bytes]:
    """要发布的文件内容；等于默认（没有卡片、生效 env）时是 None —— 删掉文件。"""
    if document.is_default:
        return None
    return _dump(document.to_file(updated_by=updated_by))


# ── 卡片操作（读当前文件 → 内存里改 → 整份写回，由接口层放进保存事务）────


_CARD_FIELDS = ("name", "account", "api_format", "base_url", "model")


def set_active(document: ProviderDocument, request: Any) -> ProviderDocument:
    """切换：只改 active，卡片原样。"""
    if not isinstance(request, Mapping):
        raise ProviderSettingsError("请求体必须是对象")
    active = _parse_active(dict(request), document.cards)
    return replace(document, active=active)


def put_card(
    document: ProviderDocument,
    card_id: str,
    request: Any,
    catalog: AccountCatalog,
    *,
    create: bool,
) -> ProviderDocument:
    """新建或编辑一张卡片。token 留空 = 沿用这张卡片已存的；新建时必填。

    新建或改挂账户时，不许指向已退役的账户；账户没变的编辑照收（备用卡片可以
    留着后来被退役的账户，启用时才拒绝）。
    """
    validate_card_id(card_id)
    if not isinstance(request, Mapping):
        raise ProviderSettingsError("请求体必须是对象")
    unknown = set(request) - set(_CARD_FIELDS) - {"token", "judge_thinking"}
    if unknown:
        raise ProviderSettingsError("请求里有不认识的字段")
    current = document.cards.get(card_id)
    if create and current is not None:
        raise ProviderSettingsError("这张卡片已经存在")
    if create and card_id in document.retired_card_ids:
        raise ProviderSettingsError("这个卡片 id 用过了，不再复用")
    if not create and current is None:
        raise KeyError(card_id)
    raw: Dict[str, Any] = {key: request.get(key) for key in _CARD_FIELDS}
    token = request.get("token")
    if token is None:
        token = ""
    if not isinstance(token, str):
        raise ProviderSettingsError("token 必须是字符串")
    if not token and current is not None:
        token = current.token.reveal()
    if not token:
        raise ProviderSettingsError("新建卡片需要填写 token")
    raw["token"] = token
    raw["judge_thinking"] = request.get("judge_thinking")
    raw["created_at"] = current.created_at if current is not None else _now()
    card = _parse_card(card_id, raw, catalog)
    if current is None or current.account != card.account:
        if catalog.get(card.account).retired:
            raise ProviderSettingsError("不能挂到已退役的账户上")
    cards = dict(document.cards)
    cards[card_id] = card
    return replace(document, cards=cards)


class CardInUse(ProviderSettingsError):
    """卡片正被生成或判分引用，不能删除。"""


def delete_card(document: ProviderDocument, card_id: str) -> ProviderDocument:
    if card_id == ENV_CARD:
        raise ProviderSettingsError("env 是内建卡片，不能删除")
    if card_id not in document.cards:
        raise KeyError(card_id)
    if card_id in document.active.cards():
        raise CardInUse("这张卡片正在被生成或判分使用，先切到别的卡片再删除")
    cards = dict(document.cards)
    del cards[card_id]
    return replace(document, cards=cards, retired_card_ids=document.retired_card_ids | {card_id})


def clear_cards(document: ProviderDocument) -> ProviderDocument:
    """删掉全部卡片、回到 env。删掉的 id 照样记着。"""
    return ProviderDocument(
        cards={}, active=DEFAULT_ACTIVE, retired_card_ids=document.taken_ids(),
    )


def accounts_in_use(settings_dir: Path, active: ModelSettings) -> Tuple[str, ...]:
    """现在被线路**实际引用**的账户：生效快照的两条线路，加上磁盘上 provider.json
    生效的卡片（两者平时一致）。备用卡片挂着的账户不算占用。

    磁盘文件坏了就抛错：这时说不清哪些账户被引用，不能放行退役。
    """
    used = {active.generation.account, active.judge.account}
    catalog = load_accounts(settings_dir)
    document = read_document(settings_dir, catalog)
    if document is not None:
        for card_id in document.active.cards():
            used.add(ENV_ACCOUNT if card_id == ENV_CARD else document.cards[card_id].account)
    return tuple(sorted(used))


def _split_url(url: str) -> Optional[SplitResult]:
    """解析失败返回 None。urlsplit 的 ValueError 会把整段 netloc（可能夹着凭据）
    拼进消息，所以原异常一律不往外传。"""
    try:
        parts = urlsplit(url)
        # 这几个属性是惰性解析的，坏端口之类在这里才抛。
        parts.hostname, parts.port, parts.username, parts.password
    except ValueError:
        return None
    return parts


# ── 文件 ─────────────────────────────────────────────────────────────────


def _read_json(path: Path, *, label: str) -> Optional[Dict[str, Any]]:
    """不存在 → None；读不出 / 不是 JSON 对象 → 固定措辞的错误，不带原文。"""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError):
        raise ProviderSettingsError(f"{label}文件读不出来") from None
    try:
        raw = json.loads(text)
    except ValueError:
        raise ProviderSettingsError(f"{label}文件不是合法的 JSON") from None
    if not isinstance(raw, dict):
        raise ProviderSettingsError(f"{label}文件顶层必须是对象")
    return raw


def _dump(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
