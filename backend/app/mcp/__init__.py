"""The MCP server BioFlow exposes at /api/v1/mcp.

Mounted in-process on the existing FastAPI app rather than run as its own
service: see docs/superpowers/specs/2026-08-06-mcp-server-design.md. Tool
functions call the service layer directly, which is what keeps a future split
into a separate container a change to those calls rather than to the tool
surface an agent sees.
"""
