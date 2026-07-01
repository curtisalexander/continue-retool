import os
import sys

# Make the shell_mcp package importable when running pytest from shell-mcp/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
