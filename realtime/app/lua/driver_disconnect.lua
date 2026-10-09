-- The WebSocket closed: the driver cannot get an offer now, so they leave the GEO sets
-- at once. Status and last_seen stay: a driver who reconnects within DRIVER_SILENCE_S
-- returns with the next point; one who does not is taken offline by the reaper.
--
-- KEYS: state, geo...  (one GEO set per tariff)
-- ARGV: driver_id, conn_id
-- Returns: ok | not_owner

local state = KEYS[1]
local driver_id, conn_id = ARGV[1], ARGV[2]

-- A newer connection has already taken the driver over (4409): it owns the state now.
if redis.call('HGET', state, 'conn_id') ~= conn_id then
    return 'not_owner'
end

for i = 2, #KEYS do
    redis.call('ZREM', KEYS[i], driver_id)
end
redis.call('HDEL', state, 'conn_id')
return 'ok'
