-- Promote jobs that have waited past their class threshold.
--
-- This is the anti-starvation guarantee. Without it, a machine under sustained
-- user load would never run maintenance -- and a `verify_files` job that never
-- runs is a *silent* failure, the worst kind.
--
-- Discrete promotion is chosen over continuous aging deliberately: it touches
-- only the jobs that actually crossed a threshold (O(promoted)) rather than
-- rescoring the entire queue every tick (O(n log n)).
--
-- KEYS[1] ready zset
-- ARGV[1] cutoff_score   -- scores <= this have waited long enough
-- ARGV[2] class_base     -- base score of the class being promoted
-- ARGV[3] target_base    -- base score of the tier above
-- ARGV[4] max_batch
--
-- Returns the number of jobs promoted.

local cutoff      = tonumber(ARGV[1])
local class_base  = tonumber(ARGV[2])
local target_base = tonumber(ARGV[3])
local max_batch   = tonumber(ARGV[4]) or 200

-- Only jobs within this class's score band are eligible.
local candidates = redis.call('ZRANGEBYSCORE', KEYS[1], class_base, cutoff,
                              'LIMIT', 0, max_batch)
local promoted = 0

for i = 1, #candidates do
  local job_id = candidates[i]
  local score = redis.call('ZSCORE', KEYS[1], job_id)
  if score then
    -- Preserve relative age within the new tier so promoted jobs stay ordered
    -- among themselves rather than all collapsing to one score.
    local new_score = target_base + (tonumber(score) - class_base)
    redis.call('ZADD', KEYS[1], new_score, job_id)
    redis.call('HSET', 'bp:job:' .. job_id, 'score', new_score, 'promoted', '1')
    promoted = promoted + 1
  end
end

return promoted
