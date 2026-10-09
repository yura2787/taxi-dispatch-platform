-- Binds a driver to a new WebSocket connection and returns the state for the snapshot.
--
-- KEYS: state
-- ARGV: conn_id, offline_ttl_s
-- Returns: the state hash as a flat field/value list.
--
-- Writing conn_id is what takes the driver over from an older connection: every
-- script that the older connection runs from now on sees a foreign conn_id.

local state = KEYS[1]
local conn_id, offline_ttl_s = ARGV[1], ARGV[2]

redis.call('HSET', state, 'conn_id', conn_id)

-- A missing status means offline. Write it down and keep the TTL every offline state
-- has, so a driver who connects and never goes online does not stay in Redis forever.
local status = redis.call('HGET', state, 'status')
if not status or status == 'offline' then
    redis.call('HSET', state, 'status', 'offline')
    redis.call('EXPIRE', state, offline_ttl_s)
end

return redis.call('HGETALL', state)
