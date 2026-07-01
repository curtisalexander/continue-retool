import os
import sys

# Make the search_mcp package importable when running pytest from search-mcp/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
