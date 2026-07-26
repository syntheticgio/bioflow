-- Atomically claim the highest-priority dispatchable job.
--
-- "Dispatchable" means: its class is currently admitted by the load governor,
-- AND its declared resource demand fits within what is still free. Selection,
-- resource reservation, and the lease grant must happen together -- doing them
-- in separate round trips lets two workers both observe the same free capacity
-- and both claim, which is exactly the bug this script exists to prevent.
--
-- KEYS[1] ready zset      KEYS[2] running zset
-- ARGV[1] now_ms          ARGV[2] lease_ms       ARGV[3] worker_id
-- ARGV[4] allowed classes (comma separated)
-- ARGV[5] cpu_free        ARGV[6] mem_mb_free    ARGV[7] io_heavy_free
-- ARGV[8] scan_limit
--
-- Returns: {job_id, class, cpu, mem_mb, io, epoch} or nil

local now_ms      = tonumber(ARGV[1])
local lease_ms    = tonumber(ARGV[2])
local worker_id   = ARGV[3]
local cpu_free    = tonumber(ARGV[5])
local mem_free    = tonumber(ARGV[6])
local io_free     = tonumber(ARGV[7])
local scan_limit  = tonumber(ARGV[8]) or 50

-- Set membership for the admitted classes.
local allowed = {}
for cls in string.gmatch(ARGV[4], "[^,]+") do
  allowed[cls] = true
end

-- Bounded scan: anything past the first `scan_limit` entries is by definition
-- lower priority, so waiting for the next tick loses nothing. This keeps claim
-- cost constant no matter how deep the queue gets.
local candidates = redis.call('ZRANGE', KEYS[1], 0, scan_limit - 1)

for i = 1, #candidates do
  local job_id = candidates[i]
  local jkey = 'bp:job:' .. job_id
  local h = redis.call('HMGET', jkey, 'class', 'cpu', 'mem_mb', 'io', 'epoch')
  local class = h[1]

  if class then
    local cpu   = tonumber(h[2]) or 1
    local mem   = tonumber(h[3]) or 0
    local io    = h[4] or 'none'
    local epoch = tonumber(h[5]) or 0

    local fits = allowed[class]
                 and cpu <= cpu_free
                 and mem <= mem_free
                 and (io ~= 'heavy' or io_free > 0)

    if fits then
      -- Fencing token: every lease grant bumps the epoch. A worker whose VM
      -- was paused past its lease expiry will still hold the old epoch, so its
      -- write-backs are rejected instead of corrupting a job another worker
      -- has since taken over.
      epoch = epoch + 1

      redis.call('ZREM', KEYS[1], job_id)
      redis.call('ZADD', KEYS[2], now_ms + lease_ms, job_id)
      redis.call('HSET', jkey,
                 'worker_id', worker_id,
                 'lease_expires', now_ms + lease_ms,
                 'started_at', now_ms,
                 'epoch', epoch)

      redis.call('INCRBY', 'bp:conc:cpu', cpu)
      redis.call('INCRBY', 'bp:conc:mem_mb', mem)
      if io == 'heavy' then
        redis.call('INCR', 'bp:conc:io_heavy')
      end

      return {job_id, class, tostring(cpu), tostring(mem), io, tostring(epoch)}
    end
  else
    -- Dispatch metadata is gone but the id is still queued. Drop it; the
    -- reconciler rebuilds from MongoDB, which is the record of truth.
    redis.call('ZREM', KEYS[1], job_id)
  end
end

return nil
