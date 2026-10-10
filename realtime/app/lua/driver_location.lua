-- Records a point that passed the checks in Python, and puts the driver into the GEO
-- set of their tariff if an order can be offered to them, or takes them out of all.
--
-- KEYS: state, last_seen, geo...  (one GEO set per tariff)
-- ARGV: driver_id, conn_id, now_ms, lat, lon, heading, speed, ts, in_zone, tariff...
--       heading and speed are '' when the phone did not report them; in_zone is 1 or 0;
--       tariffs are in the same order as the GEO keys.
-- Returns: ok | stale_connection | not_online

local state, last_seen = KEYS[1], KEYS[2]
local driver_id, conn_id, now_ms = ARGV[1], ARGV[2], ARGV[3]
local lat, lon, heading, speed, ts, in_zone = ARGV[4], ARGV[5], ARGV[6], ARGV[7], ARGV[8], ARGV[9]
local FIRST_TARIFF_ARG, FIRST_GEO_KEY = 10, 3

if redis.call('HGET', state, 'conn_id') ~= conn_id then
    return 'stale_connection'
end

local status = redis.call('HGET', state, 'status')
if not status or status == 'offline' then
    return 'not_online'
end

redis.call('HSET', state, 'lat', lat, 'lon', lon, 'ts', ts, 'updated_at', now_ms, 'in_zone', in_zone)
-- A missing field, not an empty one, means "not reported".
local function set_or_delete(field, value)
    if value == '' then
        redis.call('HDEL', state, field)
    else
        redis.call('HSET', state, field, value)
    end
end
set_or_delete('heading', heading)
set_or_delete('speed', speed)
redis.call('ZADD', last_seen, now_ms, driver_id)

-- The driver's tariff set gets the point only if an order can be offered; every other
-- set loses the driver, so they are in at most one set at any time.
local tariff = redis.call('HGET', state, 'tariff')
local offerable = status == 'available' and in_zone == '1'
for i = FIRST_GEO_KEY, #KEYS do
    local key_tariff = ARGV[FIRST_TARIFF_ARG + i - FIRST_GEO_KEY]
    if offerable and key_tariff == tariff then
        -- Redis GEO takes lon before lat.
        redis.call('GEOADD', KEYS[i], lon, lat, driver_id)
    else
        redis.call('ZREM', KEYS[i], driver_id)
    end
end
return 'ok'
