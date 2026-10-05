import random
import threading
import time
import uuid

from .store import Store
from .transports import DeliveryError, Notification, deliver


def drain(store: Store, *, env=None, limit: int = 100, max_attempts: int = 8,
          clock=time.time, sender=deliver, lease_seconds: float = 60,
          heartbeat_interval: float = 5) -> dict:
    if lease_seconds <= 0 or heartbeat_interval <= 0 or heartbeat_interval > lease_seconds / 3:
        raise ValueError("invalid_lease_configuration")
    owner = uuid.uuid4().hex
    result = {"accepted": 0, "retrying": 0, "dead": 0, "superseded": 0}
    for _ in range(limit):
        row = store.claim(owner, clock(), lease_seconds)
        if row is None:
            break
        if not store.active(row["id"], owner):
            result["superseded"] += 1
            continue
        body = row["body"]
        if clock() - row["created_at"] > 300:
            body = "This notice was queued over 5 minutes ago. Recheck the original workflow's current state.\n" + body
        notification = Notification(title=row["title"], body=body, message_id=row["message_id"],
                                    reply_reference=row["reply_reference"], reply_revision=row["reply_revision"])
        stopped = threading.Event()

        def heartbeat(delivery_id=row["id"]):
            while not stopped.wait(heartbeat_interval):
                try:
                    if not store.renew(delivery_id, owner, clock(), lease_seconds):
                        return
                except Exception:
                    # A later heartbeat can recover a transient DB lock. Never
                    # print the raw storage exception or credentials.
                    continue

        lease_thread = threading.Thread(target=heartbeat, name="aan-lease", daemon=True)
        lease_thread.start()
        try:
            evidence = sender(row["channel"], notification, env)
        except DeliveryError as exc:
            dead = not exc.retryable or row["attempts"] >= max_attempts
            state = "dead" if dead else "pending"
            delay = min(900.0, 5.0 * 2 ** min(row["attempts"] - 1, 8)) * random.uniform(1.0, 1.2)
            changed = store.finish(row["id"], owner, state=state, now=clock() + (0 if dead else delay), error=exc.code)
            result[("dead" if dead else "retrying") if changed else "superseded"] += 1
        except Exception:
            # Unknown transport failure must not disclose source exceptions.
            dead = row["attempts"] >= max_attempts
            changed = store.finish(row["id"], owner, state="dead" if dead else "pending", now=clock() + 60,
                                   error="transport_internal_error")
            result[("dead" if dead else "retrying") if changed else "superseded"] += 1
        else:
            changed = store.finish(row["id"], owner, state="accepted", now=clock(), evidence=evidence)
            result["accepted" if changed else "superseded"] += 1
        finally:
            stopped.set()
            lease_thread.join(timeout=12)
    return result
