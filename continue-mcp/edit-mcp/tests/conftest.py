import os
import sys

# Make the edit_mcp package importable when running pytest from edit-mcp/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
