from __future__ import annotations

from .policy import classify_chat
from .providers import providers
from .store import Store


def discover(store: Store) -> dict[str, int]:
    counts: dict[str, int] = {}
    total = 0
    adopted = 0
    for provider in providers():
        try:
            chats = provider.discover()
        except Exception as exc:
            store.event("discover_error", provider=provider.name, error=str(exc))
            chats = []
        counts[provider.name] = len(chats)
        total += len(chats)
        for chat in chats:
            score, state, objective = classify_chat(chat.title, chat.cwd, chat.latest_text)
            if state == "done":
                score = max(score, 1)
            store.upsert_chat(chat, score, state, objective)
            if score > 0:
                adopted += 1
    store.event("discover", total=total, adopted=adopted, by_provider=counts)
    return {"total": total, "adopted": adopted, **{f"provider_{k}": v for k, v in counts.items()}}

