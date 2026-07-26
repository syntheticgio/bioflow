// Initiates the single-node replica set on first boot.
// A replica set is required for multi-document transactions, which the CAS
// refcounting path depends on (object insert + blob refcount must be atomic).
try {
  rs.status();
} catch (e) {
  rs.initiate({
    _id: "rs0",
    members: [{ _id: 0, host: "mongo:27017" }],
  });
}
