-- Claim one tick of a periodic schedule.
--
-- Exactly one worker may win each tick. Read, compare, and advance happen
-- inside one script, so there is no window between "is it due?" and "claim it"
-- -- which a SETNX-with-TTL approach would leave open, and which would show up
-- as duplicate maintenance jobs under contention.
--
-- KEYS[1] bp:sched:next:{name}
-- ARGV[1] now_ms   ARGV[2] interval_ms   ARGV[3] catchup ("1"/"0")
--
-- Returns 1 if this caller won the tick, 0 otherwise.

local now_ms      = tonumber(ARGV[1])
local interval_ms = tonumber(ARGV[2])
local catchup     = ARGV[3] == '1'

local next_run = redis.call('GET', KEYS[1])

if not next_run then
  -- First observation: schedule the next run without firing immediately, so a
  -- restart does not trigger every schedule at once.
  redis.call('SET', KEYS[1], now_ms + interval_ms)
  return 0
end

next_run = tonumber(next_run)
if now_ms < next_run then
  return 0
end

if catchup then
  -- Fire once per missed interval.
  redis.call('SET', KEYS[1], next_run + interval_ms)
else
  -- Advance relative to now, so a four-hour laptop sleep produces exactly one
  -- tick on resume rather than 240 of them.
  redis.call('SET', KEYS[1], now_ms + interval_ms)
end

return 1
