-- offline -> available. The driver enters the GEO set only with the first point in
-- the service area (driver_location), not here: there is no position yet to put there.
--
-- KEYS: state, profile, last_seen
-- ARGV: driver_id, conn_id, now_ms, tariff...  (tariffs from the config)
-- Returns: ok | stale_connection | busy | not_eligible

local state, profile, last_seen = KEYS[1], KEYS[2], KEYS[3]
local driver_id, conn_id, now_ms = ARGV[1], ARGV[2], ARGV[3]

if redis.call('HGET', state, 'conn_id') ~= conn_id then
    return 'stale_connection'
end

local status = redis.call('HGET', state, 'status')
if status == 'offered' or status == 'on_trip' then
    return 'busy'
end

-- Checked again although the handshake did: the profile may have changed since.
local eligible, tariff = unpack(redis.call('HMGET', profile, 'eligible', 'tariff'))
local known_tariff = false
for i = 4, #ARGV do
    if ARGV[i] == tariff then
        known_tariff = true
    end
end
if eligible ~= '1' or not known_tariff then
    return 'not_eligible'
end

if status == 'available' then
    return 'ok'
end

redis.call('HSET', state, 'status', 'available', 'tariff', tariff)
-- Online from now on; without a point within DRIVER_SILENCE_S the reaper takes it back.
redis.call('ZADD', last_seen, now_ms, driver_id)
redis.call('PERSIST', state)
return 'ok'
