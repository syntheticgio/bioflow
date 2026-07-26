-- Move delayed jobs whose time has come onto the ready queue.
--
-- Covers both retry backoff and jobs scheduled for a future moment.
--
-- KEYS[1] delayed zset   KEYS[2] ready zset
-- ARGV[1] now_ms         ARGV[2] max_batch
--
-- Returns the list of job ids moved.

local now_ms    = tonumber(ARGV[1])
local max_batch = tonumber(ARGV[2]) or 100

local due = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', now_ms, 'LIMIT', 0, max_batch)
local moved = {}

for i = 1, #due do
  local job_id = due[i]
  if redis.call('ZREM', KEYS[1], job_id) == 1 then
    local score = redis.call('HGET', 'bp:job:' .. job_id, 'score')
    redis.call('ZADD', KEYS[2], tonumber(score) or now_ms, job_id)
    table.insert(moved, job_id)
  end
end

return moved
