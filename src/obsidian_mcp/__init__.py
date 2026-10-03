def main() -> None:
    # Imported here, not at the top: importing server reads VAULT_PATH and builds the MCP
    # server, which a user of obsidian_mcp.semantic alone (Nuru's indexer) doesn't want.
    from obsidian_mcp.server import main as run_server

    run_server()


__all__ = ["main"]
