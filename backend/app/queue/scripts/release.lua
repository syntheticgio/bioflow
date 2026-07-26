-- Release a running job's lease and its reserved resources.
--
-- Used on every terminal outcome (success, failure, cancellation) and when
-- requeueing on graceful shutdown. Resource counters must be released exactly
-- once: releasing twice would let the queue over-admit forever, so the ZREM
-- return value gates the decrement.
--
-- KEYS[1] running zset    KEYS[2] ready zset
-- ARGV[1] job_id
-- ARGV[2] requeue ("1" to put it back on ready, "0" to drop it)
-- ARGV[3] requeue score
--
-- Returns 1 if this call owned the release, 0 if it was already released.

local job_id  = ARGV[1]
local requeue = ARGV[2] == '1'
local score   = tonumber(ARGV[3])
local jkey    = 'bp:job:' .. job_id

local removed = redis.call('ZREM', KEYS[1], job_id)
if removed == 0 then
  -- Already released (most often by the reaper, after a lease expired).
  return 0
end

local h = redis.call('HMGET', jkey, 'cpu', 'mem_mb', 'io')
local cpu = tonumber(h[1]) or 0
local mem = tonumber(h[2]) or 0
local io  = h[3] or 'none'

redis.call('DECRBY', 'bp:conc:cpu', cpu)
redis.call('DECRBY', 'bp:conc:mem_mb', mem)
if io == 'heavy' then
  redis.call('DECR', 'bp:conc:io_heavy')
end

if requeue then
  redis.call('ZADD', KEYS[2], score, job_id)
  redis.call('HDEL', jkey, 'worker_id', 'lease_expires', 'started_at')
else
  redis.call('DEL', jkey)
  redis.call('SREM', 'bp:cancel', job_id)
end

return 1
