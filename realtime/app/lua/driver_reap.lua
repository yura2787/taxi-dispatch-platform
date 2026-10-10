-- Takes offline a driver who has been silent for too long.
--
-- KEYS: state, last_seen, geo...  (one GEO set per tariff)
-- ARGV: driver_id, cutoff_ms, offline_ttl_s
-- Returns: reaped | alive | gone
--
-- The reaper picks silent drivers first and reaps them one by one, so a point may
-- arrive in between. The silence is checked again here, atomically with the change,
-- which also makes it safe to run several reapers at once.

local state, last_seen = KEYS[1], KEYS[2]
local driver_id, cutoff_ms, offline_ttl_s = ARGV[1], ARGV[2], ARGV[3]

local score = redis.call('ZSCORE', last_seen, driver_id)
if not score then
    -- Already offline: went offline or another reaper got there first.
    return 'gone'
end
if tonumber(score) >= tonumber(cutoff_ms) then
    return 'alive'
end

-- Stage 3: a driver who is on_trip is not taken offline here; the passenger is told
-- that the connection to the driver is lost and the dispatcher gets a flag.

redis.call('HSET', state, 'status', 'offline')
redis.call('ZREM', last_seen, driver_id)
for i = 3, #KEYS do
    redis.call('ZREM', KEYS[i], driver_id)
end
redis.call('EXPIRE', state, offline_ttl_s)
return 'reaped'
