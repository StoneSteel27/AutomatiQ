"""Console entry point for the automatiq MCP server (stdio transport)."""


def main() -> None:
    from automatiq.mcp.server import main as server_main

    server_main()


if __name__ == "__main__":
    main()
