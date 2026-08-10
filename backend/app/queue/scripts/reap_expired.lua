-- Requeue jobs whose lease has expired.
--
-- A lease expires when a worker dies, hangs, or -- the common case on this
-- platform -- the Docker VM is paused because the laptop lid closed. The job
-- returns to `ready`; its epoch has already been bumped by the next claim, so
-- the original worker's writes are rejected if it ever wakes up.
--
-- KEYS[1] running zset   KEYS[2] ready zset
-- ARGV[1] now_ms         ARGV[2] max_batch
--
-- Returns a flat list: {job_id, attempts_after_increment, ...}

local now_ms    = tonumber(ARGV[1])
local max_batch = tonumber(ARGV[2]) or 100

local expired = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', now_ms, 'LIMIT', 0, max_batch)
local result = {}

for i = 1, #expired do
  local job_id = expired[i]
  local jkey = 'bp:job:' .. job_id

  if redis.call('ZREM', KEYS[1], job_id) == 1 then
    local h = redis.call('HMGET', jkey, 'cpu', 'mem_mb', 'io', 'score', 'node')
    local cpu   = tonumber(h[1]) or 0
    local mem   = tonumber(h[2]) or 0
    local io    = h[3] or 'none'
    local score = tonumber(h[4]) or now_ms
    local node  = h[5] or ''

    -- Per-node concurrency counter keys, or global keys when node is empty.
    local conc_cpu = 'bp:conc:cpu'
    local conc_mem = 'bp:conc:mem_mb'
    local conc_io  = 'bp:conc:io_heavy'
    if node ~= '' then
      conc_cpu = conc_cpu .. ':' .. node
      conc_mem = conc_mem .. ':' .. node
      conc_io  = conc_io .. ':' .. node
    end

    redis.call('DECRBY', conc_cpu, cpu)
    redis.call('DECRBY', conc_mem, mem)
    if io == 'heavy' then
      redis.call('DECR', conc_io)
    end

    local attempts = redis.call('HINCRBY', jkey, 'attempts', 1)
    redis.call('HDEL', jkey, 'worker_id', 'lease_expires', 'started_at')
    -- Re-queue at the original score so a job that has already waited does not
    -- lose its place to newer arrivals.
    redis.call('ZADD', KEYS[2], score, job_id)

    table.insert(result, job_id)
    table.insert(result, tostring(attempts))
  end
end

return result
