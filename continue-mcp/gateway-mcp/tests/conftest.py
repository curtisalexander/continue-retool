import os
import sys

# Make the gateway_mcp package importable when running pytest from gateway-mcp/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
