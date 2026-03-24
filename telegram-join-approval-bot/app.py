from __future__ import annotations

from pathlib import Path

from telegram.ext import Application, ApplicationBuilder, ChatJoinRequestHandler

from avatar_ocr import AvatarOCR
from moderator import JoinRequestModerator
from risk_terms import RiskTermsMatcher
from settings import Settings, configure_logging, load_settings


def build_application(settings: Settings) -> Application:
    """Build the PTB application with a single join request handler."""
    base_dir = Path(__file__).resolve().parent
    matcher = RiskTermsMatcher(settings, base_dir=base_dir)
    avatar_ocr = AvatarOCR(settings, matcher)
    moderator = JoinRequestModerator(settings, matcher, avatar_ocr)

    application = (
        ApplicationBuilder()
        .token(settings.bot_token)
        .concurrent_updates(True)
        .build()
    )
    application.add_handler(ChatJoinRequestHandler(moderator.handle_join_request, block=True))
    return application


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)

    application = build_application(settings)
    application.run_webhook(
        listen="0.0.0.0",
        port=settings.port,
        url_path=settings.webhook_path.lstrip("/"),
        webhook_url=settings.webhook_endpoint,
        allowed_updates=["chat_join_request"],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
