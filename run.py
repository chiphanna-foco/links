#!/usr/bin/env python3
"""Entry point — starts the uvicorn server using PORT from the environment."""
import uvicorn
from config import HOST, PORT

if __name__ == "__main__":
    uvicorn.run("server:app", host=HOST, port=PORT)
