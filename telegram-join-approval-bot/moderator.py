from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from telegram import ChatJoinRequest, Update
from telegram.ext import ContextTypes

from avatar_ocr import AvatarOCR, AvatarResult
from risk_terms import RiskTermsMatcher
from settings import Settings
from text_normalizer import normalize_text


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AvatarCacheEntry:
    ocr_text: str
    normalized_text: str
    is_text_avatar: bool
    chinese_char_count: int
    total_char_count: int
    matched_term: str | None
    decision: str
    timestamp: float


@dataclass(slots=True)
class ReviewDecision:
    final_decision: str
    reason: str
    nickname_match: str | None = None
    bio_match: str | None = None
    avatar_match: str | None = None
    avatar_result: AvatarResult | None = None


class JoinRequestModerator:
    """Core join request review workflow."""

    def __init__(
        self,
        settings: Settings,
        matcher: RiskTermsMatcher,
        avatar_ocr: AvatarOCR,
    ) -> None:
        self.settings = settings
        self.matcher = matcher
        self.avatar_ocr = avatar_ocr
        self.avatar_cache: dict[str, AvatarCacheEntry] = {}

    async def handle_join_request(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        request = update.chat_join_request
        if request is None:
            return

        decision = await self.review_join_request(request, context)
        await self.apply_decision(request, context, decision)
        self._log_decision(request, decision)

    async def review_join_request(
        self, request: ChatJoinRequest, context: ContextTypes.DEFAULT_TYPE
    ) -> ReviewDecision:
        nickname_text = "".join(
            part for part in (request.from_user.first_name, request.from_user.last_name, request.from_user.username) if part
        )
        nickname_match = self.matcher.match(nickname_text)
        if nickname_match:
            return ReviewDecision(
                final_decision="decline",
                reason="nickname_risk_term",
                nickname_match=nickname_match,
            )

        bio_match = self.matcher.match(request.bio or "")
        if bio_match:
            return ReviewDecision(
                final_decision="decline",
                reason="bio_risk_term",
                bio_match=bio_match,
            )

        if not self.settings.ocr_enabled:
            return ReviewDecision(final_decision="approve", reason="ocr_disabled")

        avatar_result = await self._check_avatar(request, context)
        if avatar_result and avatar_result.matched_term:
            return ReviewDecision(
                final_decision="decline",
                reason="avatar_text_risk_term",
                avatar_match=avatar_result.matched_term,
                avatar_result=avatar_result,
            )

        return ReviewDecision(
            final_decision="approve",
            reason="no_rule_hit",
            avatar_result=avatar_result,
        )

    async def apply_decision(
        self,
        request: ChatJoinRequest,
        context: ContextTypes.DEFAULT_TYPE,
        decision: ReviewDecision,
    ) -> None:
        kwargs = self._timeout_kwargs()
        if decision.final_decision == "decline":
            await context.bot.decline_chat_join_request(
                chat_id=request.chat.id,
                user_id=request.from_user.id,
                **kwargs,
            )
            if self.settings.decline_and_ban:
                try:
                    await context.bot.ban_chat_member(
                        chat_id=request.chat.id,
                        user_id=request.from_user.id,
                        **kwargs,
                    )
                except Exception as exc:
                    logger.warning("ban_after_decline_failed user_id=%s error=%s", request.from_user.id, exc)
            return

        await context.bot.approve_chat_join_request(
            chat_id=request.chat.id,
            user_id=request.from_user.id,
            **kwargs,
        )

    async def _check_avatar(
        self,
        request: ChatJoinRequest,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> AvatarResult | None:
        try:
            photos = await context.bot.get_user_profile_photos(
                user_id=request.from_user.id,
                limit=1,
                **self._timeout_kwargs(),
            )
        except Exception as exc:
            logger.warning("profile_photo_fetch_failed user_id=%s error=%s", request.from_user.id, exc)
            return None

        if photos.total_count <= 0 or not photos.photos:
            return None

        latest_sizes = photos.photos[0]
        if not latest_sizes:
            return None

        largest = latest_sizes[-1]
        cache_key = largest.file_unique_id
        cached = self._get_cached_avatar(cache_key)
        if cached is not None:
            return AvatarResult(
                extracted_text=cached.ocr_text,
                normalized_text=cached.normalized_text,
                is_text_avatar=cached.is_text_avatar,
                chinese_char_count=cached.chinese_char_count,
                total_char_count=cached.total_char_count,
                matched_term=cached.matched_term,
            )

        try:
            tg_file = await context.bot.get_file(largest.file_id, **self._timeout_kwargs())
            image_bytes = bytes(
                await tg_file.download_as_bytearray(**self._timeout_kwargs())
            )
        except Exception as exc:
            logger.warning("profile_photo_download_failed user_id=%s error=%s", request.from_user.id, exc)
            return None

        try:
            result = self.avatar_ocr.analyze_avatar(image_bytes)
        except Exception as exc:
            logger.warning("profile_photo_ocr_failed user_id=%s error=%s", request.from_user.id, exc)
            return None

        self.avatar_cache[cache_key] = AvatarCacheEntry(
            ocr_text=result.extracted_text,
            normalized_text=result.normalized_text,
            is_text_avatar=result.is_text_avatar,
            chinese_char_count=result.chinese_char_count,
            total_char_count=result.total_char_count,
            matched_term=result.matched_term,
            decision="decline" if result.matched_term else "approve",
            timestamp=time.time(),
        )
        self._prune_avatar_cache()
        return result

    def _get_cached_avatar(self, cache_key: str) -> AvatarResult | None:
        entry = self.avatar_cache.get(cache_key)
        if entry is None:
            return None
        if time.time() - entry.timestamp > self.settings.ocr_cache_ttl_seconds:
            self.avatar_cache.pop(cache_key, None)
            return None
        return AvatarResult(
            extracted_text=entry.ocr_text,
            normalized_text=entry.normalized_text,
            is_text_avatar=entry.is_text_avatar,
            chinese_char_count=entry.chinese_char_count,
            total_char_count=entry.total_char_count,
            matched_term=entry.matched_term,
        )

    def _prune_avatar_cache(self) -> None:
        now = time.time()
        expired = [
            key
            for key, entry in self.avatar_cache.items()
            if now - entry.timestamp > self.settings.ocr_cache_ttl_seconds
        ]
        for key in expired:
            self.avatar_cache.pop(key, None)

    def _log_decision(self, request: ChatJoinRequest, decision: ReviewDecision) -> None:
        logger.info(
            "join_review user_id=%s chat_id=%s nickname_match=%s bio_match=%s avatar_match=%s decision=%s reason=%s",
            request.from_user.id,
            request.chat.id,
            decision.nickname_match or "-",
            decision.bio_match or "-",
            decision.avatar_match or "-",
            decision.final_decision,
            decision.reason,
        )

    def _timeout_kwargs(self) -> dict[str, Any]:
        timeout = self.settings.request_timeout_seconds
        return {
            "read_timeout": timeout,
            "write_timeout": timeout,
            "connect_timeout": timeout,
            "pool_timeout": timeout,
        }
