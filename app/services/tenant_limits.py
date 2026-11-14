"""Tenant fairness: per-tenant request rate and per-tenant processing concurrency.

State lives in Redis because it is shared by every API process and every worker.
Both limits FAIL OPEN: if Redis is down we lose fairness for a while, not availability.
The database stays the source of truth for the file state; Redis only holds counters.
"""

from __future__ import annotations

import logging
import time
import uuid
from functools import lru_cache

import redis

from app.core.config import Settings, get_settings

logger = logging.getLogger("docforge.limits")


# Atomic concurrency slots. A tenant's slots are a sorted set: member = file_id, score = lease expiry.
# Expired leases are removed first, so a worker that dies without releasing its slot cannot block
# the tenant forever (the lease simply runs out).
_ACQUIRE_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]
redis.call('ZREMRANGEBYSCORE', key, '-inf', now)
if redis.call('ZSCORE', key, member) then
  redis.call('ZADD', key, now + ttl, member)
  return 1
end
if redis.call('ZCARD', key) < limit then
  redis.call('ZADD', key, now + ttl, member)
  redis.call('EXPIRE', key, math.ceil(ttl))
  return 1
end
return 0
"""

class TenantLimiter:
    def __init__(
        self,
        client: redis.Redis,
        rate_per_s: int,
        max_concurrent: int,
        slot_ttl_s: int, 
    ) -> None:
        self._client = client
        self._rate = rate_per_s
        self._max_concurrent = max_concurrent
        self._ttl = slot_ttl_s
        self._acquire = client.register_script(_ACQUIRE_LUA)


    @classmethod
    def from_settings(cls, settings: Settings) -> "TenantLimiter":
        client = redis.Redis.from_url(
            settings.redis_state_url,
            socket_timeout=0.2,    # a limiter must never become the slow part of a request
            socket_connect_timeout=0.2,
            decode_response=True,
        )
        # The lease must outlive the longest possible task, otherwise a healthy task loses its slot.
        return cls(
            client, 
            settings.tenant_rate_per_s,
            settings.tenant_max_concurrent,
            settings.task_hard_time_limit_s + 60,
        )

    def allow_request(self, tenant_id: str) -> bool:
        """Fixed-window rate limit: at most ``rate_per_s`` requests per tenant per second."""
        key = f"rate:{tenant_id}:{int(time.time())}"
        try:
            pipe = self._client.pipeline()
            pipe.incr(key)
            pipe.expire(key, 2)
            count, _ = pipe.execute()
        except redis.RedisError:
            logger.warning("rate limiter unavailable, failing open")
            return True
        return int(count) <= self._rate

    def acquire_slot(self, tenant_id: str, file_id: uuid.UUID) -> bool:
        try:
            granted = self._acquire(
                keys=[f"slots:{tenant_id}"],
                args=[time.time(), self._ttl, self._max_concurrent, str(file_id)],
            )
        except redis.RedisError:
            logger.warning("concurrency limiter unavailable, failing open")
            return True
        return bool(granted)

    def release_slot(self, tenant_id: str, file_id: uuid.UUID) -> None:
        try:
            self._client.zrem(f"slots:{tenant_id}", str(file_id))
        except redis.RedisError:
            logger.warning("could not release slot; the lease will expire by itself")


@lru_cache
def get_limiter() -> TenantLimiter:
    return TenantLimiter.from_settings(get_settings())