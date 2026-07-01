import os
import sys

# Make the package importable when running pytest from the hello-mcp/ dir.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
