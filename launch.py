"""Source-tree entry point for MCP clients. stdout belongs to MCP."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent/'src'))
from wxbg.gateway import main
if __name__=='__main__':main()
