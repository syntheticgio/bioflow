// Initiates the single-node replica set on first boot.
// A replica set is required for multi-document transactions, which the CAS
// refcounting path depends on (object insert + blob refcount must be atomic).
//
// The member is 127.0.0.1, not mongo:27017, and that is the whole point of
// this file working at all. The official entrypoint runs these scripts against
// a temporary mongod that it forces onto `--bind_ip 127.0.0.1` with
// `--bind_ip_all` stripped (docker-entrypoint.sh, "_mongod_hack_ensure_arg_val
// --bind_ip 127.0.0.1"). A node bound only to loopback does not recognise
// itself as `mongo:27017`, so initiating with that name failed every single
// time with:
//
//   MongoServerError: No host described in new configuration with
//   {version: 1, term: 0} for replica set rs0 maps to this node
//
// That failure was silent -- the entrypoint does not abort on it -- so the set
// was left uninitiated and the *healthcheck's* fallback initiated it a few
// seconds into the real boot instead. Compose's `--wait` polls health during
// that window, which is the race that made `ops/worktree-up.sh` fail roughly
// one run in three with "container ... is unhealthy" (issue #101).
//
// A loopback member name is safe here because this is a single-node set that
// is only ever reached with `directConnection=true` (see MONGO_URL in
// docker-compose.yml), which tells the driver to talk to the host it was given
// rather than to the addresses the replica set config advertises. Peer
// containers therefore still connect as `mongo:27017` and multi-document
// transactions still commit.
//
// Existing installs are untouched: the entrypoint runs initdb scripts only on
// an empty data directory, so a stack whose set is already configured as
// `mongo:27017` keeps that config and never reaches this file.
try {
  rs.status();
} catch (e) {
  rs.initiate({
    _id: "rs0",
    members: [{ _id: 0, host: "127.0.0.1:27017" }],
  });
}
