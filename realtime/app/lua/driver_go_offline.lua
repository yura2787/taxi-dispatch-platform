-- available -> offline, on the driver's request.
--
-- KEYS: state, last_seen, geo...  (one GEO set per tariff)
-- ARGV: driver_id, conn_id, offline_ttl_s
-- Returns: ok | stale_connection | not_online | busy

local state, last_seen = KEYS[1], KEYS[2]
local driver_id, conn_id, offline_ttl_s = ARGV[1], ARGV[2], ARGV[3]

if redis.call('HGET', state, 'conn_id') ~= conn_id then
    return 'stale_connection'
end

local status = redis.call('HGET', state, 'status')
if status == 'offered' or status == 'on_trip' then
    return 'busy'
end
if status ~= 'available' then
    return 'not_online'
end

redis.call('HSET', state, 'status', 'offline')
redis.call('ZREM', last_seen, driver_id)
for i = 3, #KEYS do
    redis.call('ZREM', KEYS[i], driver_id)
end
redis.call('EXPIRE', state, offline_ttl_s)
return 'ok'
