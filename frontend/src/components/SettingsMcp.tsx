import { useState } from "react";
import { SettingsNav } from "./SettingsNav";
import { useProfileStore } from "../stores/profileStore";

/**
 * The paste-ready MCP connection config.
 *
 * Load-bearing rather than polish: the profile id is a Mongo ObjectId, and
 * without this panel the feature's first step is "go find your id in the
 * database". The URL is built from `window.location.origin` so the user is
 * handed whichever port they already have open -- nobody has to learn that
 * 8000 exists alongside 5173.
 *
 * The path has two `/mcp` segments: `/api/v1/mcp` is where `mount_mcp_app`
 * (backend/app/mcp/server.py) mounts the sub-app, but the mounted app itself
 * -- `MCPServer.streamable_http_app()` -- adds its own default
 * `streamable_http_path` of `/mcp` for the actual endpoint. So the real,
 * externally-reachable URL is `/api/v1/mcp/mcp`, not `/api/v1/mcp`.
 */
export function SettingsMcp() {
  const profile = useProfileStore((s) => s.current);
  const [copied, setCopied] = useState(false);

  const url = profile
    ? `${window.location.origin}/api/v1/mcp/mcp?profile=${profile.id}`
    : "";

  const config = JSON.stringify(
    { mcpServers: { bioflow: { url } } },
    null,
    2,
  );

  const copy = async () => {
    await navigator.clipboard.writeText(config);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="settings-page">
      <SettingsNav />
      <h1>Settings · MCP</h1>
      <p className="settings-hint">
        BioFlow exposes an MCP server so an AI coding agent can browse your
        projects, ask what to run next, and launch pipelines. Paste this into
        your agent's MCP configuration.
      </p>

      {profile ? (
        <>
          <pre className="mono mcp-config">{config}</pre>
          <button className="btn" onClick={copy}>
            {copied ? "Copied" : "Copy configuration"}
          </button>
          <p className="settings-hint">
            Acting as <strong>{profile.username}</strong>. An agent connected
            with this URL sees only this profile's data and cannot switch
            profiles or delete anything.
          </p>
        </>
      ) : (
        <p className="settings-hint">
          Select a profile to see its connection URL.
        </p>
      )}
    </div>
  );
}
