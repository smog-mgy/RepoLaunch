"""Source-checkout MCP entry point, independent of the client's working directory."""
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from repolaunch.mcp_server import main

if __name__=='__main__':
    main()
