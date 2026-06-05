"""LLM 配置管理与密钥加密存储

负责：
  - 加密密钥初始化（环境变量 / 文件）
  - API Key 加密/解密/哈希/掩码
  - LLM 配置元数据读写（JSON）
  - LLM 配置卡片持久化（SQLite）
"""
import os, sys, json, logging, hashlib, base64
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_SRC = os.path.dirname(os.path.abspath(__file__))

# ---- 加密模块初始化 ----
try:
    from cryptography.fernet import Fernet
    _CRYPTO_AVAILABLE = True
except ImportError:
    _CRYPTO_AVAILABLE = False
    logger.warning("cryptography 库未安装，API Key 将以明文存储（不安全）。请运行: pip install cryptography")

# 加密密钥
_ENCRYPTION_KEY = os.environ.get("LLM_KEY_ENCRYPTION_KEY")
_fernet = None

if _CRYPTO_AVAILABLE and not _ENCRYPTION_KEY:
    _KEY_FILE = Path(_SRC).parent / "agent_data" / ".encryption_key"
    if _KEY_FILE.exists():
        _ENCRYPTION_KEY = _KEY_FILE.read_text(encoding="utf-8").strip()
        logger.info(f"🔑 从文件读取加密密钥 (key 前8位: {_ENCRYPTION_KEY[:8]})")
    else:
        _ENCRYPTION_KEY = Fernet.generate_key().decode()
        _KEY_FILE.write_text(_ENCRYPTION_KEY, encoding="utf-8")
        logger.warning(f"🔑 已自动生成加密密钥并持久化到 {_KEY_FILE} (key 前8位: {_ENCRYPTION_KEY[:8]})")

if _ENCRYPTION_KEY:
    os.environ["LLM_KEY_ENCRYPTION_KEY"] = _ENCRYPTION_KEY
    logger.info(f"📌 加密密钥已同步到环境变量 LLM_KEY_ENCRYPTION_KEY")

if _CRYPTO_AVAILABLE:
    _fernet = Fernet(_ENCRYPTION_KEY.encode() if isinstance(_ENCRYPTION_KEY, str) else _ENCRYPTION_KEY)
    logger.info(f"🔐 Fernet 加密器已初始化 (key 前8位: {_ENCRYPTION_KEY[:8] if _ENCRYPTION_KEY else 'N/A'})")


def get_fernet():
    return _fernet


def is_crypto_available():
    return _CRYPTO_AVAILABLE


# ---- 项目根目录 ----
def _get_project_root() -> Path:
    p = Path(__file__).resolve()
    for _ in range(10):
        if (p / "packages").is_dir() and (p / "RAG_DATA").is_dir():
            return p
        p = p.parent
    return Path(__file__).resolve().parent.parent.parent.parent


_PROJECT_ROOT = _get_project_root()
_DB_DIR = _PROJECT_ROOT / "agent_data"
_DB_DIR.mkdir(parents=True, exist_ok=True)
_DB_PATH = str(_DB_DIR / "conversations.db")

_CONFIG_PATH = _DB_DIR / "llm_config.json"


# ---- 加密工具 ----
def _hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()[:16]


def _encrypt_api_key(api_key: str) -> str:
    if _fernet:
        return _fernet.encrypt(api_key.encode("utf-8")).decode()
    return api_key


def _decrypt_api_key(api_key_enc: str) -> str:
    if _fernet:
        return _fernet.decrypt(api_key_enc.encode("utf-8")).decode()
    return api_key_enc


def _make_key_mask(api_key: str) -> str:
    if len(api_key) <= 12:
        return "•" * len(api_key)
    return api_key[:8] + "•" * (len(api_key) - 12) + api_key[-4:]


# ---- LLM 配置元数据（JSON）----
def _load_llm_config() -> dict:
    if _CONFIG_PATH.exists():
        try:
            return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        except:
            pass
    return {"current": None, "providers": {}}


def _save_llm_config(data: dict):
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---- SQLite 密钥管理 ----
def _get_db_path() -> str:
    return _DB_PATH


def _save_llm_key(provider: str, api_key: str):
    import sqlite3
    api_key_hash = _hash_api_key(api_key)
    api_key_enc = _encrypt_api_key(api_key) if _CRYPTO_AVAILABLE else api_key
    api_key_mask = _make_key_mask(api_key)
    with sqlite3.connect(_DB_PATH) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO llm_provider_keys 
               (provider, api_key_hash, api_key_enc, api_key_mask, updated_at)
               VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (provider, api_key_hash, api_key_enc, api_key_mask),
        )
    logger.info(f"✅ API Key 已加密存储到 SQLite: {provider} ({api_key_mask})")


def _get_llm_key(provider: str) -> Optional[str]:
    import sqlite3
    with sqlite3.connect(_DB_PATH) as conn:
        row = conn.execute(
            "SELECT api_key_enc, api_key_hash FROM llm_provider_keys WHERE provider = ?",
            (provider,),
        ).fetchone()
    if not row:
        return None
    api_key_enc, stored_hash = row
    if not api_key_enc:
        return None
    try:
        api_key = _decrypt_api_key(api_key_enc) if _CRYPTO_AVAILABLE else api_key_enc
        if _hash_api_key(api_key) != stored_hash:
            logger.error(f"⚠️ {provider} API Key 哈希校验失败，数据可能已损坏")
            return None
        return api_key
    except Exception as e:
        logger.error(f"⚠️ 解密 {provider} API Key 失败: {e}")
        return None


def _get_llm_key_mask(provider: str) -> Optional[str]:
    import sqlite3
    with sqlite3.connect(_DB_PATH) as conn:
        row = conn.execute(
            "SELECT api_key_mask FROM llm_provider_keys WHERE provider = ?",
            (provider,),
        ).fetchone()
    return row[0] if row else None


def _has_llm_key(provider: str) -> bool:
    return _get_llm_key_mask(provider) is not None


def _delete_llm_key(provider: str):
    import sqlite3
    with sqlite3.connect(_DB_PATH) as conn:
        conn.execute("DELETE FROM llm_provider_keys WHERE provider = ?", (provider,))
    logger.info(f"🗑️ {provider} API Key 已从 SQLite 删除")


# ---- LLM 配置卡片持久化（8 种模块类型）----
_CONFIG_CARD_MODULES = [
    "chat", "jailbreak", "scoring", "fallback", "chunk", "promptEval",
    "embedding", "reranker",
]


def _save_llm_config_card(module_id: str, provider: str, model: str, base_url: str, api_key: str):
    import sqlite3
    if not api_key and provider:
        fallback_key = _get_llm_key(provider)
        if fallback_key:
            api_key = fallback_key
    api_key_hash = _hash_api_key(api_key) if api_key else ""
    api_key_enc = _encrypt_api_key(api_key) if (api_key and _CRYPTO_AVAILABLE) else (api_key or "")
    api_key_mask = _make_key_mask(api_key) if api_key else ""
    with sqlite3.connect(_DB_PATH) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO llm_configs
               (module_id, provider, model, base_url, api_key_enc, api_key_hash, api_key_mask, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (module_id, provider, model, base_url, api_key_enc, api_key_hash, api_key_mask),
        )
    logger.info(f"✅ LLM 配置卡片已保存: {module_id} ({provider}/{model}) [{api_key_mask or '无Key'}]")
    return api_key_mask


def _get_all_llm_configs() -> dict:
    import sqlite3
    result = {}
    try:
        with sqlite3.connect(_DB_PATH) as conn:
            rows = conn.execute(
                "SELECT module_id, provider, model, base_url, api_key_mask, updated_at FROM llm_configs"
            ).fetchall()
            for row in rows:
                module_id, provider, model, base_url, api_key_mask, updated_at = row
                result[module_id] = {
                    "provider": provider or "",
                    "model": model or "",
                    "base_url": base_url or "",
                    "api_key_mask": api_key_mask or "",
                    "updated_at": updated_at or "",
                }
    except sqlite3.OperationalError:
        pass
    return result


def _get_llm_config_card(module_id: str) -> dict:
    import sqlite3
    try:
        with sqlite3.connect(_DB_PATH) as conn:
            row = conn.execute(
                "SELECT provider, model, base_url, api_key_enc, api_key_hash FROM llm_configs WHERE module_id = ?",
                (module_id,),
            ).fetchone()
        if not row:
            return {}
        provider, model, base_url, api_key_enc, stored_hash = row
        api_key = ""
        if api_key_enc and _CRYPTO_AVAILABLE:
            try:
                decrypted = _decrypt_api_key(api_key_enc)
                if _hash_api_key(decrypted) == stored_hash:
                    api_key = decrypted
            except Exception:
                pass
        return {
            "provider": provider or "",
            "model": model or "",
            "base_url": base_url or "",
            "api_key": api_key,
        }
    except sqlite3.OperationalError:
        return {}
