-- Atomically claim the highest-priority dispatchable job.
--
-- "Dispatchable" means: its class is currently admitted by the load governor,
-- AND its declared resource demand fits within what is still free. Selection,
-- resource reservation, and the lease grant must happen together -- doing them
-- in separate round trips lets two workers both observe the same free capacity
-- and both claim, which is exactly the bug this script exists to prevent.
--
-- Free headroom is computed *inside* this script from a live read of the
-- `bp:conc:*` counters, not from a value the caller precomputed. A caller-
-- supplied free value is a snapshot from moments earlier; Redis serializes
-- concurrent claim.lua executions, but a snapshot argument does not become
-- fresher just because its script runs later. Reading the counters here
-- means every execution sees the reservations of every execution before it,
-- including ones that landed after the caller took its snapshot.
--
-- KEYS[1] ready zset      KEYS[2] running zset
-- ARGV[1] now_ms          ARGV[2] lease_ms       ARGV[3] worker_id
-- ARGV[4] allowed classes (comma separated)
-- ARGV[5] cpu_budget      ARGV[6] mem_mb_budget  ARGV[7] io_heavy_budget
-- ARGV[8] scan_limit      ARGV[9] ignore_reservations ("1" or "0")
--
-- ignore_reservations is the caller's in-flight self-healing clamp: a worker
-- with nothing running cannot still owe a reservation, so when true the live
-- counters are not read at all and the full budget is offered. This mirrors
-- worker.compute_free_resources' in_flight==0 branch, which cannot be
-- replicated in-script because in_flight is per-worker local state, not
-- anything Redis holds.
--
-- Returns: {job_id, class, cpu, mem_mb, io, epoch} or nil

local now_ms      = tonumber(ARGV[1])
local lease_ms    = tonumber(ARGV[2])
local worker_id   = ARGV[3]
local cpu_budget  = tonumber(ARGV[5])
local mem_budget  = tonumber(ARGV[6])
local io_budget   = tonumber(ARGV[7])
local scan_limit  = tonumber(ARGV[8]) or 50
local ignore_reservations = ARGV[9] == '1'

-- Set membership for the admitted classes.
local allowed = {}
for cls in string.gmatch(ARGV[4], "[^,]+") do
  allowed[cls] = true
end

-- Live headroom, read as part of this same atomic execution. A negative
-- reading (a missed release drove a counter below zero) is clamped to zero
-- reserved rather than read as extra free capacity, matching _as_int on the
-- Python side.
local reserved_cpu = 0
local reserved_mem = 0
local reserved_io  = 0
if not ignore_reservations then
  local counters = redis.call('MGET', 'bp:conc:cpu', 'bp:conc:mem_mb', 'bp:conc:io_heavy')
  reserved_cpu = math.max(tonumber(counters[1]) or 0, 0)
  reserved_mem = math.max(tonumber(counters[2]) or 0, 0)
  reserved_io  = math.max(tonumber(counters[3]) or 0, 0)
end

-- Floors mirror compute_free_resources: at least 1 CPU so a fully-reserved
-- queue still drains, memory and io_heavy floor at zero since offering
-- phantom capacity is the over-admission this script exists to prevent.
local cpu_free = math.max(cpu_budget - reserved_cpu, 1)
local mem_free = math.max(mem_budget - reserved_mem, 0)
local io_free  = math.max(io_budget - reserved_io, 0)

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
