from __future__ import annotations

from .policy import classify_chat
from .providers import providers
from .store import Store


def discover(store: Store) -> dict[str, int]:
    counts: dict[str, int] = {}
    total = 0
    for provider in providers():
        try:
            chats = provider.discover()
        except Exception as exc:
            store.event("discover_error", provider=provider.name, error=str(exc))
            chats = []
        counts[provider.name] = len(chats)
        total += len(chats)
        for chat in chats:
            _score, state, objective = classify_chat(chat.title, chat.cwd, chat.latest_text)
            # All discovered chats get score=1 so they're visible; queue controls what gets worked on
            store.upsert_chat(chat, 1, state, objective)
    store.event("discover", total=total, by_provider=counts)
    return {"total": total, **{f"provider_{k}": v for k, v in counts.items()}}

