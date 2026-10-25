# pns/runtime/provider_settings.py — 模型线路、计费账户与凭据的边界（CFG-1）
#
# 两条线路：**生成**（角色开口）和**判分**（Router 验收）。每条线路是一组
# { 账户, 协议, Base URL, 模型名, token }；判分线路可以"跟随生成"。线路挂在一个
# 计费账户下，余额属于账户、token 属于线路。
#
# 线路有两个来源：
#
#   * env 派生 —— 面板从没存过（`settings/provider.json` 不存在）。两条线路的协议 /
#     地址 / key 都来自 .env 那一套，模型名分别是 `GENERATOR_MODEL or MODEL` 与
#     `EVALUATOR_MODEL or MODEL`，**判分是独立线路，不是跟随**（与改动前逐字段
#     一致）。key 照旧在用到时才从进程环境取，不进快照。
#   * 面板 —— `settings/provider.json`。文件存在但读不出、过不了校验、版本不认识，
#     这份配置整体不通过（生产启动因此失败，fail-closed）。
#
# 账户目录 `settings/accounts.json` 是账户的唯一真相源；provider.json 只引用 id。
#
# 密钥边界：token 一律包在 `Secret` 里，`repr` / `str` 输出 "<redacted>"、不能被
# pickle；只有建 client 的地方 `.reveal()`。对外的一切（API、reload 状态、审计）
# 只走各对象的 `public_dict()`，token 只给"有没有"和够长时的末 4 位。
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.parse import SplitResult, urlsplit

from pns.runtime.settings_files import durable_write

PROVIDER_FILE = "provider.json"
ACCOUNTS_FILE = "accounts.json"
PROVIDER_VERSION = 1
ACCOUNTS_VERSION = 1

LINE_GENERATION = "generation"
LINE_JUDGE = "judge"
LINES = (LINE_GENERATION, LINE_JUDGE)

API_FORMATS = ("anthropic", "openai")

# `env` 是内建账户：代表 .env 那把 key 背后的计费账户。env 派生的两条线路都归它。
ENV_ACCOUNT = "env"
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

    @property
    def host(self) -> str:
        parts = _split_url(self.base_url)
        return (parts.hostname or "") if parts is not None else ""

    def public_dict(self) -> Dict[str, Any]:
        return {
            "account": self.account,
            "api_format": self.api_format,
            "base_url": self.base_url,
            "model": self.model,
            "has_token": bool(self.token),
            "token_hint": self.token.hint(),
        }


@dataclass(frozen=True)
class ModelSettings:
    """一份配置快照里的模型设定：两条解析好的线路 + 判分阈值。"""

    generation: Line
    judge: Line
    judge_follows: bool
    source: str
    ooc_threshold: float
    # env 那一套凭据（协议 / 地址 / key / 模型）在当前进程环境里是否完整。
    # 面板凭据时这关系到"能不能直接回滚到旧镜像"（§9）。
    env_complete: bool = True
    # env 那把 key 的变量名（PNS_API_KEY_NAME）。env 派生线路从它取 key；面板
    # 线路不用它，但回滚预检要知道旧镜像会去找哪个变量。
    env_key_name: str = "MIMO_API_KEY"

    @classmethod
    def from_env(cls, env: Mapping[str, str], *, ooc_threshold: float = 5.0) -> "ModelSettings":
        """只看 env 的派生结果（等于面板从没存过时的设定）。"""
        generation, judge = env_lines(env)
        return cls(
            generation=generation, judge=judge, judge_follows=False, source=SOURCE_ENV,
            ooc_threshold=ooc_threshold, env_complete=env_is_complete(env),
            env_key_name=env_settings(env)[2],
        )

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
            "generation": self.generation.public_dict(),
            "judge": {
                "follow": LINE_GENERATION if self.judge_follows else None,
                **self.judge.public_dict(),
            },
            "generator_model": self.generator_model,
            "evaluator_model": self.evaluator_model,
            "ooc_threshold": self.ooc_threshold,
            "env_complete": self.env_complete,
        }

    to_dict = public_dict


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


def env_lines(env: Mapping[str, str]) -> Tuple[Line, Line]:
    """文件不存在时的两条线路。判分是**独立**线路，模型取 EVALUATOR_MODEL。"""
    api_format, base_url, key_name, provider = env_settings(env)
    model = env.get("MODEL", "mimo-v2.6-pro")
    token = EnvSecret(key_name)

    def line(name: str, line_model: str) -> Line:
        return Line(
            name=name, account=ENV_ACCOUNT, api_format=api_format,
            base_url=base_url, model=line_model, token=token, provider=provider,
        )

    return (
        line(LINE_GENERATION, env.get("GENERATOR_MODEL") or model),
        line(LINE_JUDGE, env.get("EVALUATOR_MODEL") or model),
    )


def env_is_complete(env: Mapping[str, str]) -> bool:
    """旧镜像在生产上能不能只靠这套 env 起来（它只检查协议合法和 key 非空）。"""
    try:
        _, _, key_name, _ = env_settings(env)
    except ProviderSettingsError:
        return False
    return bool(env.get(key_name))


def _parse_line(name: str, raw: Any, catalog: AccountCatalog) -> Line:
    label = "生成线路" if name == LINE_GENERATION else "判分线路"
    if not isinstance(raw, dict):
        raise ProviderSettingsError(f"{label} 必须是对象")
    fields = {}
    for key in ("account", "api_format", "base_url", "model", "token"):
        value = raw.get(key)
        if not isinstance(value, str):
            raise ProviderSettingsError(f"{label} 缺少字段 {key}")
        fields[key] = value
    account = catalog.get(fields["account"])
    return Line(
        name=name,
        account=fields["account"],
        api_format=fields["api_format"],
        base_url=fields["base_url"],
        model=fields["model"],
        token=Secret(fields["token"]),
        provider=account.kind if account is not None else "",
    )


def validate_line(line: Line, catalog: AccountCatalog, *, require_token: bool = True) -> None:
    """§6 有效线路校验。启动、PUT、DELETE 共用。"""
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
        # URL 里夹凭据会原样出现在公开投影里。
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


def validate_settings(settings: ModelSettings, catalog: AccountCatalog) -> None:
    validate_line(settings.generation, catalog)
    validate_line(settings.judge, catalog)


def load_model_settings(
    settings_dir: Path, env: Mapping[str, str], *, ooc_threshold: float
) -> Tuple[ModelSettings, AccountCatalog]:
    """读出这份配置的模型设定。面板文件存在就以它为准，否则 env 派生。

    面板文件做完整的结构校验（坏文件 = 配置不通过）；token 是否为空也在这里
    拦，因为面板线路的 token 只可能来自这个文件。env 派生只做改动前那几项
    检查 —— key 缺不缺由生产启动检查和建世界的地方分别把关，开发环境没有 key
    也要能起来。面板文件存在时不校验 env（它只决定 env_complete）。
    """
    catalog = load_accounts(settings_dir)
    raw = _read_json(Path(settings_dir) / PROVIDER_FILE, label="线路配置")
    if raw is None:
        return ModelSettings.from_env(env, ooc_threshold=ooc_threshold), catalog
    if raw.get("version") != PROVIDER_VERSION:
        raise ProviderSettingsError("线路配置的版本不认识")
    generation = _parse_line(LINE_GENERATION, raw.get("generation"), catalog)
    judge_raw = raw.get("judge")
    follows = isinstance(judge_raw, dict) and set(judge_raw) == {"follow"}
    if follows:
        if judge_raw["follow"] != LINE_GENERATION:
            raise ProviderSettingsError("判分线路只能跟随生成线路")
        judge = Line(
            name=LINE_JUDGE, account=generation.account, api_format=generation.api_format,
            base_url=generation.base_url, model=generation.model, token=generation.token,
            provider=generation.provider,
        )
    else:
        judge = _parse_line(LINE_JUDGE, judge_raw, catalog)
    settings = ModelSettings(
        generation=generation, judge=judge, judge_follows=follows,
        source=SOURCE_PANEL, ooc_threshold=ooc_threshold,
        # 旧 env 不完整只影响能不能回滚，不否决当前合法的面板线路。
        env_complete=env_is_complete(env),
        env_key_name=env.get("PNS_API_KEY_NAME", "MIMO_API_KEY"),
    )
    validate_settings(settings, catalog)
    return settings, catalog


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
