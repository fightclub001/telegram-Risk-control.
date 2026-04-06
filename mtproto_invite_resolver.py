from __future__ import annotations

import asyncio
import html
import os
import re
import time
from typing import Any
from urllib.parse import unquote


_INVITE_PATTERNS = (
    re.compile(r"(?:https?://)?(?:t|telegram)\.(?:me|dog)/(?:joinchat/|\+)([\w-]+)", re.IGNORECASE),
    re.compile(r"tg://join\?(?:[^#]*&)?invite=([\w-]+)", re.IGNORECASE),
    re.compile(r"(?:^|[?&])invite=([\w-]+)(?:$|[&#])", re.IGNORECASE),
)


def _normalize_text(text: str) -> str:
    normalized = html.unescape((text or "").strip())
    normalized = normalized.replace("\\/", "/")
    for _ in range(2):
        decoded = unquote(normalized)
        if decoded == normalized:
            break
        normalized = decoded
    return normalized


class MTProtoInviteResolver:
    """Resolve private Telegram invite hashes to real chat IDs via MTProto when possible."""

    def __init__(self, *, data_dir: str) -> None:
        self.api_id = int((os.getenv("MT_API_ID") or "0").strip() or "0")
        self.api_hash = (os.getenv("MT_API_HASH") or "").strip()
        self.session_string = (os.getenv("MT_SESSION_STRING") or "").strip()
        self.session_file = (os.getenv("MT_SESSION_FILE") or os.path.join(data_dir, "mtproto_user")).strip()
        self.request_timeout = max(5, int((os.getenv("MT_REQUEST_TIMEOUT_SECONDS") or "10").strip()))
        self.enabled = bool(self.api_id and self.api_hash and (self.session_string or self.session_file))
        self.disabled_reason = "" if self.enabled else "mtproto_not_configured"
        self._client: Any | None = None
        self._lock = asyncio.Lock()
        self._cache: dict[str, tuple[float, int | None, str]] = {}
        self._cache_hit_ttl = max(1800, int((os.getenv("MT_INVITE_CACHE_HIT_TTL_SECONDS") or "86400").strip()))
        self._cache_miss_ttl = max(300, int((os.getenv("MT_INVITE_CACHE_MISS_TTL_SECONDS") or "3600").strip()))
        self._cache_max = max(64, int((os.getenv("MT_INVITE_CACHE_MAX") or "2048").strip()))

    def extract_hashes(self, text: str) -> list[str]:
        raw = _normalize_text(text)
        if not raw:
            return []
        found: list[str] = []
        seen: set[str] = set()
        for pattern in _INVITE_PATTERNS:
            for match in pattern.finditer(raw):
                invite_hash = (match.group(1) or "").strip()
                if not invite_hash or invite_hash in seen:
                    continue
                seen.add(invite_hash)
                found.append(invite_hash)
        return found

    async def resolve_text(self, text: str) -> tuple[set[int], str]:
        if not self.enabled:
            return set(), self.disabled_reason or "mtproto_not_configured"
        hashes = self.extract_hashes(text)
        if not hashes:
            return set(), "no_invite_hash"
        resolved: set[int] = set()
        reasons: list[str] = []
        for invite_hash in hashes:
            chat_id, reason = await self.resolve_hash(invite_hash)
            if chat_id is not None:
                resolved.add(int(chat_id))
            reasons.append(reason)
        if resolved:
            return resolved, "resolved:" + ",".join(str(item) for item in sorted(resolved))
        return set(), ";".join(reasons) if reasons else "invite_unresolved"

    async def resolve_hash(self, invite_hash: str) -> tuple[int | None, str]:
        now = time.time()
        cached = self._cache.get(invite_hash)
        if cached and now < float(cached[0]):
            return cached[1], cached[2]

        client = await self._ensure_client()
        if client is None:
            return None, self.disabled_reason or "mtproto_unavailable"

        try:
            from telethon.tl.functions.messages import CheckChatInviteRequest
            from telethon.utils import get_peer_id

            result = await client(CheckChatInviteRequest(invite_hash))
            chat = getattr(result, "chat", None)
            if chat is None:
                self._set_cache(invite_hash, None, "invite_no_chat_id", self._cache_miss_ttl)
                return None, "invite_no_chat_id"
            chat_id = int(get_peer_id(chat))
            self._set_cache(invite_hash, chat_id, f"invite_chat:{chat_id}", self._cache_hit_ttl)
            return chat_id, f"invite_chat:{chat_id}"
        except Exception as e:
            reason = f"invite_resolve_failed:{type(e).__name__}"
            self._set_cache(invite_hash, None, reason, self._cache_miss_ttl)
            return None, reason

    async def _ensure_client(self) -> Any | None:
        if not self.enabled:
            return None
        if self._client is not None:
            return self._client
        async with self._lock:
            if self._client is not None:
                return self._client
            try:
                from telethon import TelegramClient
                from telethon.sessions import StringSession

                if self.session_string:
                    session = StringSession(self.session_string)
                else:
                    session = self.session_file

                client = TelegramClient(
                    session,
                    self.api_id,
                    self.api_hash,
                    connection_retries=1,
                    request_retries=1,
                    auto_reconnect=False,
                    flood_sleep_threshold=0,
                    device_model="risk-control-resolver",
                    system_version="railway",
                    app_version="1.0",
                )
                await asyncio.wait_for(client.connect(), timeout=self.request_timeout)
                authorized = await asyncio.wait_for(client.is_user_authorized(), timeout=self.request_timeout)
                if not authorized:
                    self.disabled_reason = "mtproto_session_not_authorized"
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                    return None
                self._client = client
                self.disabled_reason = ""
                return self._client
            except Exception as e:
                self.disabled_reason = f"mtproto_init_failed:{type(e).__name__}"
                return None

    def _set_cache(self, invite_hash: str, chat_id: int | None, reason: str, ttl: int) -> None:
        self._cache[invite_hash] = (time.time() + max(60, int(ttl)), chat_id, reason)
        self._prune_cache()

    def _prune_cache(self) -> None:
        now = time.time()
        expired = [key for key, value in list(self._cache.items()) if now >= float(value[0])]
        for key in expired:
            self._cache.pop(key, None)
        if len(self._cache) <= self._cache_max:
            return
        overflow = sorted(self._cache.items(), key=lambda item: float(item[1][0]))[: len(self._cache) - self._cache_max]
        for key, _value in overflow:
            self._cache.pop(key, None)

